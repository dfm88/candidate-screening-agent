"""The core around a conversation: ingest, accepting candidate messages, and one worker per conversation that runs
the turns. The database lives at this edge; domain.py and agent.py stay pure."""

import asyncio
import secrets
import time
import uuid
from datetime import UTC, datetime
from functools import partial

import logfire
from pydantic_ai import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits
from pydantic_core import to_jsonable_python
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from screening.agent import history_from_turns, prompt_version, run_turn, write_summary
from screening.channels import Channel
from screening.config import ClientConfig, Language, Locale, Settings
from screening.contract import Author, ChatEvent, ChatMessage, ChatStatus, Typing
from screening.db import Candidate, Message, MessageKind, Source, Turn
from screening.domain import Application, ScreeningState, Status

ENDED = frozenset({Status.COMPLETED, Status.WITHDRAWN})


def application_of(candidate: Candidate) -> Application:
    return Application(
        full_name=candidate.full_name, vacancy_location_id=candidate.vacancy_location_id, language=candidate.language
    )


class ScreeningService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        config: ClientConfig,
        settings: Settings,
        model: Model,
        channel: Channel,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.config = config
        self.settings = settings
        self.model = model
        self.channel = channel
        self._workers: dict[uuid.UUID, asyncio.Task[None]] = {}
        # Conversations that got a message after their worker last looked: it looks again before it stops.
        self._kicked: set[uuid.UUID] = set()

    async def apply(
        self,
        *,
        external_id: str,
        source: Source,
        full_name: str,
        phone: str,
        vacancy_location_id: str,
        language: Language,
        whatsapp_opt_in: bool,
    ) -> Candidate:
        """Store an application and send the first message (a template). The same external_id returns the same
        candidate, so a resubmitted application never starts a second chat."""
        async with self.sessionmaker() as session:
            existing = await session.scalar(select(Candidate).where(Candidate.external_id == external_id))
            if existing:
                return existing

            application = Application(full_name=full_name, vacancy_location_id=vacancy_location_id, language=language)
            location = self.config.locations_by_id[vacancy_location_id]
            locale = Locale.of(language, location.country)
            bubbles = self.config.render("first_message", locale, first_name=application.first_name, city=location.city)
            state = ScreeningState(status=Status.INVITED, language=language)
            candidate = Candidate(
                id=uuid.uuid4(),
                external_id=external_id,
                source=source,
                full_name=full_name,
                phone=phone,
                vacancy_location_id=vacancy_location_id,
                language=language,
                whatsapp_opt_in=whatsapp_opt_in,
                chat_token=secrets.token_urlsafe(24),
                status=state.status,
                outcome=None,
                state=state.model_dump(mode="json"),
                summary=None,
                last_outbound_at=datetime.now(UTC),
            )
            # Our first message is part of the conversation the agent sees, like any message we send.
            history: list[ModelMessage] = [ModelResponse(parts=[TextPart("\n\n".join(bubbles))])]
            try:
                session.add(candidate)
                await session.flush()
                turn = Turn(
                    candidate_id=candidate.id, messages=ModelMessagesTypeAdapter.dump_python(history, mode="json")
                )
                session.add(turn)
                await session.flush()
                quick_replies = [self.config.label(locale, "start"), self.config.label(locale, "not_interested")]
                session.add_all(
                    Message(
                        candidate_id=candidate.id,
                        author=Author.ASSISTANT,
                        kind=MessageKind.TEMPLATE,
                        template_key="first_message",
                        text=bubble,
                        # Under the last bubble only.
                        quick_replies=quick_replies if number == len(bubbles) - 1 else [],
                        turn_id=turn.id,
                    )
                    for number, bubble in enumerate(bubbles)
                )
                await session.commit()
            except IntegrityError:
                # The same application submitted twice at once: the other request stored it.
                await session.rollback()
                return (await session.scalars(select(Candidate).where(Candidate.external_id == external_id))).one()
        return candidate

    async def find(self, chat_token: str) -> Candidate | None:
        async with self.sessionmaker() as session:
            return await session.scalar(select(Candidate).where(Candidate.chat_token == chat_token))

    async def messages_after(self, candidate_id: uuid.UUID, after_id: int) -> list[ChatMessage]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Message).where(Message.candidate_id == candidate_id, Message.id > after_id).order_by(Message.id)
            )
            return [ChatMessage.model_validate(row) for row in rows]

    async def receive(self, candidate: Candidate, text: str, client_id: str) -> ChatMessage:
        """Store a candidate message, echo it, and wake the conversation's worker. A retried send is stored once."""
        now = datetime.now(UTC)
        async with self.sessionmaker() as session:
            same = select(Message).where(Message.candidate_id == candidate.id, Message.client_id == client_id)
            message = await session.scalar(same)
            if message is None:
                message = Message(
                    candidate_id=candidate.id,
                    author=Author.CANDIDATE,
                    kind=MessageKind.TEXT,
                    text=text,
                    client_id=client_id,
                )
                session.add(message)
                await session.execute(
                    update(Candidate)
                    .where(Candidate.id == candidate.id)
                    .values(last_inbound_at=now, first_reply_at=func.coalesce(Candidate.first_reply_at, now))
                )
                try:
                    await session.commit()
                except IntegrityError:
                    # The same send, retried while the first was being stored.
                    await session.rollback()
                    message = (await session.scalars(same)).one()
        echo = ChatMessage.model_validate(message)
        await self.channel.publish(candidate.id, [echo])
        self.ensure_worker(candidate.id)
        return echo

    def ensure_worker(self, candidate_id: uuid.UUID) -> None:
        """One worker per conversation, so its turns never overlap and a burst of messages gets one reply."""
        self._kicked.add(candidate_id)
        worker = self._workers.get(candidate_id)
        if worker is None or worker.done():
            worker = asyncio.create_task(self._work(candidate_id), name=f"worker-{candidate_id}")
            worker.add_done_callback(partial(self._worker_done, candidate_id))
            self._workers[candidate_id] = worker

    def _worker_done(self, candidate_id: uuid.UUID, worker: asyncio.Task[None]) -> None:
        # The callback runs a moment after the task ends, so a new message may already have started the next worker.
        if self._workers.get(candidate_id) is worker:
            del self._workers[candidate_id]
        # A worker is an edge: a crash outside the turn (the database, say) is recorded, and the messages stay pending
        # for the next message or a restart.
        if not worker.cancelled() and (error := worker.exception()):
            logfire.error("{worker} crashed", worker=worker.get_name(), _exc_info=error)

    async def resume(self) -> None:
        """At startup: answer what was still pending when the last process stopped (or a turn failed)."""
        async with self.sessionmaker() as session:
            pending = select(Message.candidate_id).where(Message.author == Author.CANDIDATE, Message.turn_id.is_(None))
            for candidate_id in await session.scalars(pending.distinct()):
                self.ensure_worker(candidate_id)

    async def stop(self) -> None:
        # Cancelled turns leave their messages pending; resume() picks them up at the next start. A copy, because
        # each worker removes itself once it's done.
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _work(self, candidate_id: uuid.UUID) -> None:
        """Let a burst of messages settle, answer everything pending in one turn, and repeat until nothing is."""
        while True:
            self._kicked.discard(candidate_id)
            await asyncio.sleep(self.settings.chat_debounce_seconds)
            async with self.sessionmaker() as session:
                candidate = await session.get_one(Candidate, candidate_id)
                pending = list(
                    await session.scalars(
                        select(Message)
                        .where(
                            Message.candidate_id == candidate_id,
                            Message.author == Author.CANDIDATE,
                            Message.turn_id.is_(None),
                        )
                        .order_by(Message.id)
                    )
                )
                stored = list(
                    await session.scalars(
                        select(Turn.messages).where(Turn.candidate_id == candidate_id).order_by(Turn.id)
                    )
                )
            if not pending:
                # A message stored while we were looking has kicked us: look again rather than leave it unanswered.
                if candidate_id in self._kicked:
                    continue
                return

            await self.channel.publish(candidate_id, [Typing(active=True)])
            state = ScreeningState.model_validate(candidate.state)
            application = application_of(candidate)
            now = datetime.now(UTC)
            started = time.perf_counter()
            try:
                result = await run_turn(
                    state=state,
                    application=application,
                    config=self.config,
                    candidate_messages=[message.text for message in pending],
                    history=history_from_turns(
                        [ModelMessagesTypeAdapter.validate_python(messages) for messages in stored],
                        self.settings.max_history_messages,
                    ),
                    model=self.model,
                    now=now,
                    conversation_id=str(candidate_id),
                    model_settings=ModelSettings(request_timeout=self.settings.request_timeout),
                    turn_timeout=self.settings.turn_timeout,
                    usage_limits=UsageLimits(request_limit=self.settings.turn_request_limit),
                )
            except Exception as error:
                # Every fallback model failed too, the turn ran out of time (RunTimedOut) or of requests
                # (UsageLimitExceeded). The messages stay pending: the candidate's next message or a restart retries
                # them.
                logfire.exception("turn failed for {candidate_id}", candidate_id=candidate_id)
                notice = await self._tell_failure(candidate, state, pending, error)
                await self.channel.publish(candidate_id, [*notice, Typing(active=False)])
                return

            async with self.sessionmaker() as session, session.begin():
                turn = Turn(
                    candidate_id=candidate_id,
                    messages=ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
                    prompt_version=prompt_version(self.config),
                    models=[m.model_name or "unknown" for m in result.messages if isinstance(m, ModelResponse)],
                    usage=to_jsonable_python(result.usage),
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
                session.add(turn)
                await session.flush()
                await session.execute(
                    update(Message).where(Message.id.in_([message.id for message in pending])).values(turn_id=turn.id)
                )
                replies = [
                    Message(
                        candidate_id=candidate_id,
                        author=Author.ASSISTANT,
                        kind=MessageKind.TEXT,
                        text=text,
                        turn_id=turn.id,
                    )
                    for text in result.reply
                ]
                session.add_all(replies)
                row = await session.get_one(Candidate, candidate_id)
                row.state = result.state.model_dump(mode="json")
                row.status = result.state.status
                row.outcome = result.state.outcome
                if replies:
                    row.last_outbound_at = now
                if result.state.status is Status.COMPLETED and candidate.status is not Status.COMPLETED:
                    row.completed_at = now

            events: list[ChatEvent] = [ChatMessage.model_validate(reply) for reply in replies]
            if result.state.status is not candidate.status:
                events.append(ChatStatus(status=result.state.status))
            await self.channel.publish(candidate_id, [*events, Typing(active=False)])
            if result.state.status in ENDED and candidate.status not in ENDED:
                await self._summarize(candidate_id, result.state, application, now)

    async def _tell_failure(
        self, candidate: Candidate, state: ScreeningState, pending: list[Message], error: Exception
    ) -> list[ChatMessage]:
        """The "give me a moment" notice, a template because the model is what's failing. Sent once while the same
        messages wait, not on every retry. The failed turn is recorded either way."""
        async with self.sessionmaker() as session, session.begin():
            turn = Turn(
                candidate_id=candidate.id, messages=[], prompt_version=prompt_version(self.config), error=repr(error)
            )
            session.add(turn)
            await session.flush()
            told = await session.scalar(
                select(Message.id)
                .where(
                    Message.candidate_id == candidate.id,
                    Message.template_key == "failure",
                    Message.id > pending[0].id,
                )
                .limit(1)
            )
            if told is not None:
                return []
            location = self.config.locations_by_id[candidate.vacancy_location_id]
            notice = [
                Message(
                    candidate_id=candidate.id,
                    author=Author.ASSISTANT,
                    kind=MessageKind.TEMPLATE,
                    template_key="failure",
                    text=bubble,
                    turn_id=turn.id,
                )
                for bubble in self.config.render("failure", Locale.of(state.language, location.country))
            ]
            session.add_all(notice)
        return [ChatMessage.model_validate(message) for message in notice]

    async def _summarize(
        self, candidate_id: uuid.UUID, state: ScreeningState, application: Application, now: datetime
    ) -> None:
        """The recruiter summary, written once the screening ends. If it fails, the key data still show."""
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Message).where(Message.candidate_id == candidate_id).order_by(Message.id)
            )
            transcript = "\n".join(
                f"{'Candidate' if row.author is Author.CANDIDATE else 'Assistant'}: {row.text}" for row in rows
            )
        try:
            summary = await write_summary(
                state,
                application,
                self.config,
                transcript,
                self.model,
                now,
                str(candidate_id),
                model_settings=ModelSettings(request_timeout=self.settings.request_timeout),
            )
        except Exception:
            logfire.exception("summary failed for {candidate_id}", candidate_id=candidate_id)
            return
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(Candidate).where(Candidate.id == candidate_id).values(summary=summary.model_dump(mode="json"))
            )

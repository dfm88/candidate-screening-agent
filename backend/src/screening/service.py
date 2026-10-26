"""The core around a conversation: ingest, accepting candidate messages, one worker per conversation that runs the
turns, and the clock's work (tick). The database lives at this edge; domain.py and agent.py stay pure."""

import asyncio
import dataclasses
import secrets
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial

import logfire
from pydantic_ai import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_core import to_jsonable_python
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from screening.agent import (
    Medium,
    TurnResult,
    asks_something,
    call_history,
    history_from_turns,
    prompt_version,
    run_turn,
    stream_turn,
    write_summary,
)
from screening.channels import Channel
from screening.config import ClientConfig, Language, Locale, Settings
from screening.contract import (
    Author,
    ChatEvent,
    ChatMessage,
    ChatStatus,
    Corrected,
    DropOffStage,
    MessageKind,
    Metrics,
    Tab,
    Typing,
)
from screening.db import Candidate, Message, Source, Turn
from screening.domain import ENDED, Application, Outcome, ScreeningState, Status, transition

# How often the lifespan calls tick().
TICK_SECONDS = 30
# Any constant, the same in every instance: two of them overlap while Cloud Run rolls out a revision.
TICK_LOCK = 7_350_201
# Pending messages older than this, with no worker on them, are retried by tick(). A live turn ends within
# TURN_TIMEOUT (35 s), so they're left by a failed turn or a stopped process.
RETRY_AFTER = timedelta(seconds=60)
# Failed turns on the same pending messages before tick() stops retrying them: a turn that always fails (one that hits
# the request limit, say) would cost a model run every minute.
MAX_FAILED_TURNS = 3
# Runs in a row a text turn throws away because a new message came in while they went: a candidate who keeps typing
# still gets a reply.
MAX_THROWN_AWAY = 2

PENDING = and_(Message.author == Author.CANDIDATE, Message.turn_id.is_(None))
# The rest are about the candidate of the query they're used in. Correlated explicitly: auto-correlation only reaches
# the query right around a subquery, and OLDEST_PENDING sits two levels down inside REPLIES_FAILED, where it would
# join every candidate instead.
OLDEST_PENDING = (
    select(func.min(Message.created_at))
    .where(Message.candidate_id == Candidate.id, PENDING)
    .correlate(Candidate)
    .scalar_subquery()
)
# Our turns keep failing on the candidate's messages: tick() has stopped retrying, and the recruiter sees it in Review.
# The candidate's next message or a restart tries again.
REPLIES_FAILED = (
    select(func.count())
    .where(Turn.candidate_id == Candidate.id, Turn.error.is_not(None), Turn.created_at >= OLDEST_PENDING)
    .correlate(Candidate)
    .scalar_subquery()
    >= MAX_FAILED_TURNS
)
# The recruiter's tabs. A decision is final, so a decided candidate stays where the decision put it whatever the chat
# does next, unless our replies keep failing: someone has to look at that either way. Withdrawn is closed whatever
# happened. Abandoned and not qualified fall through to closed.
TAB = case(
    (Candidate.status == Status.WITHDRAWN, Tab.CLOSED.value),
    (REPLIES_FAILED, Tab.REVIEW.value),
    (Candidate.decided_outcome == Outcome.QUALIFIED, Tab.READY.value),
    (Candidate.decided_outcome.is_not(None), Tab.CLOSED.value),
    (Candidate.status.in_([Status.PENDING, Status.INVITED, Status.IN_PROGRESS]), Tab.IN_PROGRESS.value),
    (Candidate.outcome == Outcome.QUALIFIED, Tab.READY.value),
    (Candidate.outcome == Outcome.NEEDS_HUMAN_REVIEW, Tab.REVIEW.value),
    else_=Tab.CLOSED.value,
)
# The recruiter's list shows the latest matches; search reaches the rest.
LIST_LIMIT = 200


def application_of(candidate: Candidate) -> Application:
    return Application(
        full_name=candidate.full_name, vacancy_location_id=candidate.vacancy_location_id, language=candidate.language
    )


def render_reminder(config: ClientConfig, key: str, state: ScreeningState, application: Application) -> list[str]:
    """A reminder as tick() sends it. Mid-chat it picks up where the chat stopped: the next field to ask, or the recap.
    The evals send it the same way."""
    location = config.locations_by_id[application.vacancy_location_id]
    locale = Locale.of(state.language, location.country)
    stage = next(iter(state.still_to_ask), None)
    return config.render(
        key,
        locale,
        first_name=application.first_name,
        city=location.city,
        stage=config.label(locale, f"field_{stage}" if stage else "stage_recap"),
    )


async def store_template(
    session: AsyncSession, candidate_id: uuid.UUID, key: str, bubbles: list[str], quick_replies: Sequence[str] = ()
) -> list[Message]:
    """A message we send from a template, stored as a turn of its own: the agent sees everything we send."""
    history: list[ModelMessage] = [ModelResponse(parts=[TextPart("\n\n".join(bubbles))])]
    turn = Turn(candidate_id=candidate_id, messages=ModelMessagesTypeAdapter.dump_python(history, mode="json"))
    session.add(turn)
    await session.flush()
    messages = [
        Message(
            candidate_id=candidate_id,
            author=Author.ASSISTANT,
            kind=MessageKind.TEMPLATE,
            template_key=key,
            text=bubble,
            # Under the last bubble only.
            quick_replies=list(quick_replies) if number == len(bubbles) - 1 else [],
            turn_id=turn.id,
        )
        for number, bubble in enumerate(bubbles)
    ]
    session.add_all(messages)
    return messages


@dataclass
class VoiceTurn:
    """A turn of a call, once stored."""

    turn_id: int
    # Model requests the turn made, for the call's latency log.
    requests: int


@dataclass
class StoredLine:
    """A line of a call as stored. Its client id ends with its position in the call's transcript."""

    message_id: int
    author: Author
    text: str


@dataclass
class Correction:
    """A stored line of a call, against what the candidate heard: our line cut to the part played, or `heard=None`
    when none of it was; or the candidate's words, transcribed again."""

    message_id: int
    heard: str | None


@dataclass
class Tick:
    """What one tick did, by candidate."""

    abandoned: list[uuid.UUID] = field(default_factory=list)
    reminded: list[uuid.UUID] = field(default_factory=list)
    retried: list[uuid.UUID] = field(default_factory=list)
    summarized: list[uuid.UUID] = field(default_factory=list)


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
        # Conversations whose call ended on a line of ours asking nothing: their worker runs a turn with nothing
        # pending (pick_up_after_call).
        self._pick_ups: set[uuid.UUID] = set()
        # A conversation's turns never overlap: the text worker and a voice call both take its lock.
        self._locks: defaultdict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Text turns whose run a new message would supersede (receive()): None, or when the newest such message came
        # in (time.monotonic()).
        self._running: dict[uuid.UUID, float | None] = {}
        # Summaries written in the background, held so they aren't garbage-collected while they run.
        self._background: set[asyncio.Task[None]] = set()
        # Screenings this process has tried to summarize. tick() writes the summaries a restart lost; one that failed
        # here is tried again after the next restart, not every tick.
        self._summarized: set[uuid.UUID] = set()

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
            try:
                session.add(candidate)
                await session.flush()
                quick_replies = [self.config.label(locale, "start"), self.config.label(locale, "not_interested")]
                await store_template(session, candidate.id, "first_message", bubbles, quick_replies)
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

    async def candidates(
        self, tab: Tab | None, query: str | None
    ) -> tuple[list[tuple[Candidate, Tab, bool]], dict[Tab, int]]:
        """The recruiter's list, newest first, with whether our replies keep failing, and how many candidates each tab
        holds."""
        found = select(Candidate, TAB, REPLIES_FAILED).order_by(Candidate.created_at.desc()).limit(LIST_LIMIT)
        if tab is not None:
            # TAB is a SQL expression, not a constant: flipped as SIM300 wants, it would type as a bool.
            found = found.where(TAB == tab)  # noqa: SIM300
        if query:
            found = found.where(
                or_(
                    Candidate.full_name.icontains(query, autoescape=True),
                    Candidate.phone.icontains(query, autoescape=True),
                )
            )
        # Grouped by the label: Postgres can't tell that two copies of the CASE, with their own parameters, are equal.
        tab_column = TAB.label("tab")
        async with self.sessionmaker() as session:
            rows = (await session.execute(found)).tuples().all()
            counts = (await session.execute(select(tab_column, func.count()).group_by(tab_column))).tuples().all()
        return (
            [(candidate, Tab(tab), replies_failed) for candidate, tab, replies_failed in rows],
            {Tab(tab): count for tab, count in counts},
        )

    async def candidate(self, candidate_id: uuid.UUID) -> tuple[Candidate, Tab, bool, list[Message]] | None:
        """One candidate for the recruiter, with whether our replies keep failing, and the whole chat."""
        async with self.sessionmaker() as session:
            found = select(Candidate, TAB, REPLIES_FAILED).where(Candidate.id == candidate_id)
            row = (await session.execute(found)).tuples().first()
            if row is None:
                return None
            messages = await session.scalars(
                select(Message).where(Message.candidate_id == candidate_id).order_by(Message.id)
            )
            candidate, tab, replies_failed = row
            return candidate, Tab(tab), replies_failed, list(messages)

    async def metrics(self) -> Metrics:
        """The recruiter page's metrics, from the candidates as they stand. A screening counts as finished once it has
        a rules' outcome, which it keeps if the candidate withdraws afterwards."""
        finished = Candidate.outcome.is_not(None)
        # A rules' outcome of qualified or not qualified that a recruiter has decided on. Most not-qualified ones sit
        # in Closed unopened: counting them would make overrides look rarer than they are.
        checked = and_(
            Candidate.outcome.in_([Outcome.QUALIFIED, Outcome.NOT_QUALIFIED]), Candidate.decided_outcome.is_not(None)
        )
        totals = select(
            func.count(),
            func.count(Candidate.first_reply_at),
            func.count().filter(finished),
            func.count().filter(Candidate.outcome == Outcome.NEEDS_HUMAN_REVIEW),
            func.count().filter(checked),
            # Against the rules' outcome as it stands. After a decision it can only move to review (a changed knockout
            # answer, a request for a person), so a later change can hide an override but never make one up.
            func.count().filter(checked, Candidate.decided_outcome != Candidate.outcome),
            func.percentile_cont(0.5).within_group(Candidate.completed_at - Candidate.created_at).filter(finished),
        ).select_from(Candidate)
        abandoned = select(Candidate.state, Candidate.first_reply_at).where(Candidate.status == Status.ABANDONED)
        async with self.sessionmaker() as session:
            applied, replied, outcomes, reviews, checked_outcomes, overrides, median = (
                (await session.execute(totals)).tuples().one()
            )
            stopped = (await session.execute(abandoned)).tuples().all()
        drop_off: Counter[DropOffStage] = Counter(
            # Where the reminders stopped: the next field to ask, or the recap.
            next(iter(ScreeningState.model_validate(state).still_to_ask), "recap") if replied_at else "not_started"
            for state, replied_at in stopped
        )
        return Metrics(
            applied=applied,
            response_rate=replied / applied if applied else None,
            completion_rate=outcomes / replied if replied else None,
            drop_off=drop_off,
            review_rate=reviews / outcomes if outcomes else None,
            overrides=overrides,
            override_rate=overrides / checked_outcomes if checked_outcomes else None,
            median_seconds_to_outcome=round(median.total_seconds()) if median else None,
        )

    async def decide(
        self, candidate_id: uuid.UUID, outcome: Outcome, note: str | None, decided_by: str
    ) -> Candidate | None:
        """Record the recruiter's decision on a finished screening and tell the candidate with a template. The decision
        is final: None when there already is one, or the screening hasn't finished."""
        now = datetime.now(UTC)
        async with self.sessionmaker() as session, session.begin():
            # One conditional statement, so two recruiters deciding at once can't both send the template.
            candidate = await session.scalar(
                update(Candidate)
                .where(
                    Candidate.id == candidate_id,
                    Candidate.status == Status.COMPLETED,
                    Candidate.decided_outcome.is_(None),
                )
                .values(
                    decided_outcome=outcome,
                    decided_by=decided_by,
                    decision_note=note,
                    decided_at=now,
                    last_outbound_at=now,
                )
                .returning(Candidate)
            )
            if candidate is None:
                return None
            state = ScreeningState.model_validate(candidate.state)
            location = self.config.locations_by_id[candidate.vacancy_location_id]
            key = f"decision_{outcome}"
            bubbles = self.config.render(
                key,
                Locale.of(state.language, location.country),
                first_name=application_of(candidate).first_name,
                city=location.city,
            )
            messages = await store_template(session, candidate.id, key, bubbles)
        await self.channel.publish(candidate.id, [ChatMessage.model_validate(message) for message in messages])
        return candidate

    async def receive(
        self, candidate: Candidate, text: str, client_id: str, kind: MessageKind = MessageKind.TEXT
    ) -> ChatMessage:
        """Store a candidate message (typed, or a voice note as transcribed), echo it, and wake the conversation's
        worker. A retried send is stored once. A new message supersedes a text turn whose run is still going, so the
        turn runs again with it (_work)."""
        now = datetime.now(UTC)
        async with self.sessionmaker() as session:
            same = select(Message).where(Message.candidate_id == candidate.id, Message.client_id == client_id)
            message = await session.scalar(same)
            if message is None:
                message = Message(
                    candidate_id=candidate.id,
                    author=Author.CANDIDATE,
                    kind=kind,
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
                else:
                    # Only a message stored now: a retried send brings nothing new, so it doesn't throw a run away.
                    if candidate.id in self._running:
                        self._running[candidate.id] = time.monotonic()
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
            for candidate_id in await session.scalars(select(Message.candidate_id).where(PENDING).distinct()):
                self.ensure_worker(candidate_id)

    async def tick(self, now: datetime) -> Tick:
        """The clock's work, every TICK_SECONDS: *abandoned* after the last reminder, reminders to silent candidates,
        the timed retry of messages a failed turn left pending, and the summaries a restart lost. `now` is the
        caller's, so tests move the clock. While another instance ticks, this one skips."""
        done = Tick()
        timings = self.config.timings
        reminders = {
            Status.INVITED: ("reminder_not_started", timings.reminders_not_started),
            Status.IN_PROGRESS: ("reminder_in_progress", timings.reminders_in_progress),
        }
        sent: list[tuple[uuid.UUID, list[Message]]] = []
        # Its queries run twice a minute and would bury the turns in the traces; what it did is logged below.
        with logfire.suppress_instrumentation():
            async with self.sessionmaker() as session, session.begin():
                if not await session.scalar(select(func.pg_try_advisory_xact_lock(TICK_LOCK))):
                    return done
                # Waiting for the candidate, with nothing of theirs left to answer.
                silent = select(Candidate).where(Candidate.status.in_(list(reminders)), OLDEST_PENDING.is_(None))

                abandoned = silent.where(Candidate.last_outbound_at <= now - timings.abandon_after)
                for candidate in await session.scalars(abandoned):
                    state = ScreeningState.model_validate(candidate.state)
                    state.status = transition(state.status, Status.ABANDONED)
                    candidate.state = state.model_dump(mode="json")
                    candidate.status = state.status
                    done.abandoned.append(candidate.id)

                due = or_(
                    *(
                        and_(
                            Candidate.status == status,
                            Candidate.reminders_sent == number,
                            Candidate.last_outbound_at <= now - delay,
                        )
                        for status, (_, delays) in reminders.items()
                        for number, delay in enumerate(delays)
                    )
                )
                # The session flushes before this query, so the candidates just abandoned are left out.
                for candidate in await session.scalars(silent.where(due)):
                    location = self.config.locations_by_id[candidate.vacancy_location_id]
                    if timings.quiet_hours.contain(now.astimezone(location.tz).time()):
                        # Still due on the next tick, until the quiet hours end.
                        continue
                    key, delays = reminders[candidate.status]
                    state = ScreeningState.model_validate(candidate.state)
                    bubbles = render_reminder(self.config, key, state, application_of(candidate))
                    sent.append((candidate.id, await store_template(session, candidate.id, key, bubbles)))
                    silence_began = candidate.last_outbound_at
                    assert silence_began is not None  # the due condition compares it
                    # One reminder per tick: a late tick (after a restart, say) skips the ones it missed.
                    candidate.reminders_sent = sum(silence_began + delay <= now for delay in delays)
                    done.reminded.append(candidate.id)

                waiting = select(Candidate.id).where(now - RETRY_AFTER >= OLDEST_PENDING, ~REPLIES_FAILED)
                done.retried = [
                    candidate_id for candidate_id in await session.scalars(waiting) if candidate_id not in self._workers
                ]
                unsummarized = select(Candidate).where(Candidate.status.in_(ENDED), Candidate.summary.is_(None))
                lost = [
                    candidate
                    for candidate in await session.scalars(unsummarized)
                    # A running worker writes its own summary once it has published the close.
                    if candidate.id not in self._summarized and candidate.id not in self._workers
                ]

        # Out of the suppression: the workers and summaries started here inherit this context, and get traced.
        for candidate_id, messages in sent:
            await self.channel.publish(candidate_id, [ChatMessage.model_validate(message) for message in messages])
        for candidate_id in done.retried:
            self.ensure_worker(candidate_id)
        for candidate in lost:
            state = ScreeningState.model_validate(candidate.state)
            await self._summarize(candidate.id, state, application_of(candidate), now)
            done.summarized.append(candidate.id)
        if any(asdict(done).values()):
            logfire.info("tick", **asdict(done))
        return done

    async def stop(self) -> None:
        # Cancelled turns leave their messages pending; resume() picks them up at the next start. A copy, because
        # each worker removes itself once it's done.
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _work(self, candidate_id: uuid.UUID) -> None:
        """Let a burst of messages settle, answer everything pending in one turn, and repeat until nothing is. A run
        that a new message came in during (receive()) is thrown away when it ends: nothing is stored or sent, and after
        the wait from that message the turn runs again with it too, up to MAX_THROWN_AWAY times in a row. Once the run
        has ended, a new message waits for the next turn."""
        # From a burst's first message; after a run thrown away, from the newest message that came in during it.
        wait = self.settings.chat_debounce_seconds
        thrown_away = 0
        # The turn stored after them counts what they used, so the cost per screening stays exact.
        thrown_away_usage = RunUsage()
        while True:
            self._kicked.discard(candidate_id)
            await asyncio.sleep(wait)
            wait = self.settings.chat_debounce_seconds
            async with self._locks[candidate_id]:
                async with self.sessionmaker() as session:
                    candidate = await session.get_one(Candidate, candidate_id)
                    pending = list(
                        await session.scalars(
                            select(Message).where(Message.candidate_id == candidate_id, PENDING).order_by(Message.id)
                        )
                    )
                    stored = list(
                        await session.scalars(
                            select(Turn.messages).where(Turn.candidate_id == candidate_id).order_by(Turn.id)
                        )
                    )
                picking_up = candidate_id in self._pick_ups
                self._pick_ups.discard(candidate_id)
                if not pending and not picking_up:
                    # A message stored while we were looking has kicked us: look again rather than leave it
                    # unanswered.
                    if candidate_id in self._kicked:
                        continue
                    return

                await self.channel.publish(candidate_id, [Typing(active=True)])
                state = ScreeningState.model_validate(candidate.state)
                # A voice note among them: the turn reads the voice rules, and its reply is played.
                voice_note = any(message.kind is MessageKind.VOICE_NOTE for message in pending)
                medium = Medium.VOICE_NOTE if voice_note else Medium.TEXT
                now = datetime.now(UTC)
                started = time.perf_counter()
                if thrown_away < MAX_THROWN_AWAY:
                    self._running[candidate_id] = None
                try:
                    # Ours, because the run's own spans have ended by the time we know whether it was superseded.
                    with logfire.span("chat run {candidate_id}", candidate_id=candidate_id) as run_span:
                        result = await run_turn(
                            state=state,
                            application=application_of(candidate),
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
                            # Read from its own column on every turn, so a decision made since the last one counts.
                            decision=candidate.decided_outcome,
                            medium=medium,
                        )
                        superseded_at = self._running.pop(candidate_id, None)
                        run_span.set_attribute("superseded", superseded_at is not None)
                    if superseded_at is not None:
                        # Its tools changed only the state in memory, and nothing was sent: the next run starts from
                        # the same state, and the typing indicator stays on.
                        thrown_away += 1
                        thrown_away_usage += result.usage
                        wait = superseded_at + self.settings.chat_debounce_seconds - time.monotonic()
                        continue
                    result = dataclasses.replace(result, usage=result.usage + thrown_away_usage)
                    async with self.sessionmaker() as session, session.begin():
                        turn = await self._record_turn(session, candidate, result, now, started)
                        await session.execute(
                            update(Message)
                            .where(Message.id.in_([message.id for message in pending]))
                            .values(turn_id=turn.id)
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
                except Exception as error:
                    # Every fallback model failed too, the turn ran out of time (RunTimedOut) or of requests
                    # (UsageLimitExceeded), or storing it failed. The messages stay pending: the candidate's next
                    # message, a restart or tick() retries them, and the failed turn counts towards tick()'s cap.
                    logfire.exception("turn failed for {candidate_id}", candidate_id=candidate_id)
                    notice = await self._tell_failure(candidate, state, pending, error)
                    await self.channel.publish(candidate_id, [*notice, Typing(active=False)])
                    return
                finally:
                    # The run failed, which is a failure whether superseded or not: no mark stays behind.
                    self._running.pop(candidate_id, None)

                thrown_away, thrown_away_usage = 0, RunUsage()
                events: list[ChatEvent] = [ChatMessage.model_validate(reply) for reply in replies]
                if result.state.status is not candidate.status:
                    events.append(ChatStatus(status=result.state.status))
                await self.channel.publish(candidate_id, [*events, Typing(active=False)])
            # Outside the lock: a call can go on while the summary is written.
            if result.state.status in ENDED and candidate.status not in ENDED:
                await self._summarize(candidate_id, result.state, application_of(candidate), now)

    async def voice_turn(
        self,
        candidate_id: uuid.UUID,
        *,
        call_id: str,
        history_until: int,
        call: Sequence[tuple[str, str]],
        said: list[str],
        new_lines: dict[int, tuple[str, str]],
    ) -> AsyncGenerator[str | VoiceTurn]:
        """One turn of a live call (voice.py): its text as it's written, then the turn as stored. Under the
        conversation's lock like a text turn, so the two never overlap. The model reads the turns stored up to
        `history_until` (before the call), then the call so far from ElevenLabs' transcript (`call`: our lines cut to
        what was played), then what the candidate just `said`; the state comes from the database, as always.

        Once the run has its result, the turn is stored with the call's lines that no stored turn holds yet
        (`new_lines`, by their position in the transcript: the candidate's words, and what was heard of turns cut
        off), and the reply after them. Closed before then, by a newer transcript or the end of the call, it stores
        nothing, like a failed text turn: whatever it would have saved is still in the transcript the next turn reads,
        and the context block says it isn't saved."""
        async with self._locks[candidate_id]:
            async with self.sessionmaker() as session:
                candidate = await session.get_one(Candidate, candidate_id)
                stored = list(
                    await session.scalars(
                        select(Turn.messages)
                        .where(Turn.candidate_id == candidate_id, Turn.id <= history_until)
                        .order_by(Turn.id)
                    )
                )
            await self.channel.publish(candidate_id, [Typing(active=True)])
            now = datetime.now(UTC)
            started = time.perf_counter()
            result: TurnResult | None = None
            try:
                # Closed here, in the call's task, however the call stops reading it, so its run is cancelled at
                # once and never outlives the lock.
                async with aclosing(
                    stream_turn(
                        state=ScreeningState.model_validate(candidate.state),
                        application=application_of(candidate),
                        config=self.config,
                        candidate_messages=said,
                        history=history_from_turns(
                            [ModelMessagesTypeAdapter.validate_python(messages) for messages in stored],
                            self.settings.max_history_messages,
                        )
                        + call_history(call),
                        model=self.model,
                        now=now,
                        conversation_id=str(candidate_id),
                        model_settings=ModelSettings(request_timeout=self.settings.request_timeout),
                        turn_timeout=self.settings.turn_timeout,
                        usage_limits=UsageLimits(request_limit=self.settings.turn_request_limit),
                        decision=candidate.decided_outcome,
                    )
                ) as stream:
                    async for item in stream:
                        if isinstance(item, TurnResult):
                            result = item
                        else:
                            yield item
            except Exception as error:
                # Every fallback model failed too, or the turn ran out of time or requests. Only the failure is
                # stored, as in the chat, and the call says the chat's "give me a moment" rather than go quiet. The
                # turn on the candidate's next words answers these too, since ElevenLabs' transcript still holds them,
                # and stores the line as heard.
                logfire.exception("voice turn failed for {candidate_id}", candidate_id=candidate_id)
                async with self.sessionmaker() as session, session.begin():
                    session.add(
                        Turn(
                            candidate_id=candidate_id,
                            messages=[],
                            prompt_version=prompt_version(self.config),
                            error=repr(error),
                        )
                    )
                state = ScreeningState.model_validate(candidate.state)
                location = self.config.locations_by_id[candidate.vacancy_location_id]
                yield " ".join(self.config.render("failure", Locale.of(state.language, location.country)))
                return
            finally:
                if result is None:
                    await self.channel.publish(candidate_id, [Typing(active=False)])
            assert result is not None

            async with self.sessionmaker() as session, session.begin():
                if earlier := [new_lines[position] for position in sorted(new_lines) if position < len(call)]:
                    # What the model read of the call that no stored turn holds (the candidate's words, what was heard
                    # of turns cut off): a turn of its own, like a template's, so the history after the call is the
                    # call as the model read it, and a correction of this turn's reply leaves it alone.
                    history = ModelMessagesTypeAdapter.dump_python(call_history(earlier), mode="json")
                    session.add(Turn(candidate_id=candidate_id, messages=history))
                    await session.flush()
                turn = await self._record_turn(session, candidate, result, now, started)
                lines = self._call_lines(candidate_id, call_id, new_lines, turn.id)
                session.add_all(lines)
                # Flushed first, so the call's lines get lower ids than the reply.
                await session.flush()
                reply = None
                if result.reply:
                    reply = Message(
                        candidate_id=candidate_id,
                        author=Author.ASSISTANT,
                        kind=MessageKind.VOICE,
                        text=result.reply[0],
                        client_id=f"{call_id}:{len(call) + len(said)}",
                        turn_id=turn.id,
                    )
                    session.add(reply)
                if any(line.author is Author.CANDIDATE for line in lines):
                    row = await session.get_one(Candidate, candidate_id)
                    row.last_inbound_at = now
                    row.first_reply_at = row.first_reply_at or now

            events: list[ChatEvent] = [ChatMessage.model_validate(line) for line in lines]
            if reply is not None:
                events.append(ChatMessage.model_validate(reply))
            if result.state.status is not candidate.status:
                events.append(ChatStatus(status=result.state.status))
            await self.channel.publish(candidate_id, [*events, Typing(active=False)])
        if result.state.status in ENDED and candidate.status not in ENDED:
            # In the background: the call's reply is complete, and the summary takes a model request of its own.
            summary = asyncio.create_task(
                self._summarize(candidate_id, result.state, application_of(candidate), now),
                name=f"summary-{candidate_id}",
            )
            self._background.add(summary)
            summary.add_done_callback(self._background.discard)
        yield VoiceTurn(turn_id=turn.id, requests=result.usage.requests)

    async def our_message(self, candidate_id: uuid.UUID, message_id: int) -> Message | None:
        """One of our messages in the candidate's chat, to be spoken."""
        async with self.sessionmaker() as session:
            return await session.scalar(
                select(Message).where(
                    Message.id == message_id, Message.candidate_id == candidate_id, Message.author == Author.ASSISTANT
                )
            )

    async def call_record(self, candidate_id: uuid.UUID, call_id: str) -> dict[int, StoredLine]:
        """What a call has stored so far, by position in its transcript."""
        prefix = f"{call_id}:"
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Message).where(Message.candidate_id == candidate_id, Message.client_id.startswith(prefix))
            )
            return {
                int((row.client_id or "").removeprefix(prefix)): StoredLine(row.id, row.author, row.text)
                for row in rows
            }

    async def end_call(self, candidate_id: uuid.UUID, call_id: str, lines: dict[int, tuple[str, str]]) -> None:
        """A call is over: its lines that no turn stored. The candidate's words wait for the text worker like a
        message typed in the chat, and its reply lands in the chat; ours stay until ElevenLabs' history of the call
        says how much of them was heard."""
        if not lines:
            return
        async with self.sessionmaker() as session, session.begin():
            rows = self._call_lines(candidate_id, call_id, lines, turn_id=None)
            session.add_all(rows)
            if any(row.author is Author.CANDIDATE for row in rows):
                now = datetime.now(UTC)
                await session.execute(
                    update(Candidate)
                    .where(Candidate.id == candidate_id)
                    .values(last_inbound_at=now, first_reply_at=func.coalesce(Candidate.first_reply_at, now))
                )
        await self.channel.publish(candidate_id, [ChatMessage.model_validate(row) for row in rows])
        if any(row.author is Author.CANDIDATE for row in rows):
            self.ensure_worker(candidate_id)

    async def pick_up_after_call(self, candidate_id: uuid.UUID, call_id: str) -> None:
        """Once a call is over and its record kept to what was heard: if the screening is open and our last line in
        the call asks nothing (cut off, a wait, a turn that stopped short), the chat picks up from there, so it doesn't
        sit silent. A text turn with no new messages, which the prompt says comes after a call. Words of the
        candidate's that no turn answered get their own turn instead (end_call), which picks up too."""
        record = await self.call_record(candidate_id, call_id)
        ours = [line.text for _, line in sorted(record.items()) if line.author is Author.ASSISTANT]
        async with self.sessionmaker() as session:
            candidate = await session.get_one(Candidate, candidate_id)
            pending = await session.scalar(
                select(func.count()).select_from(Message).where(Message.candidate_id == candidate_id, PENDING)
            )
        if not record or pending or candidate.status in ENDED or (ours and asks_something(ours[-1])):
            return
        logfire.info("call ended on a line asking nothing: the chat picks up", candidate_id=candidate_id)
        self._pick_ups.add(candidate_id)
        self.ensure_worker(candidate_id)

    def _call_lines(
        self, candidate_id: uuid.UUID, call_id: str, lines: dict[int, tuple[str, str]], turn_id: int | None
    ) -> list[Message]:
        """A call's lines as messages, by position: the candidate's answered by `turn_id` (pending if None), and ours
        from turns that didn't finish, which are no turn's reply. ElevenLabs ends a line of ours cut off with "..."."""
        return [
            Message(
                candidate_id=candidate_id,
                author=Author.CANDIDATE if role == "user" else Author.ASSISTANT,
                kind=MessageKind.VOICE,
                text=text,
                # The same line is never stored twice in a call.
                client_id=f"{call_id}:{position}",
                turn_id=turn_id if role == "user" else None,
                interrupted=role != "user" and text.rstrip().endswith(("...", "…")),
            )
            for position, (role, text) in sorted(lines.items())
        ]

    async def correct_heard(self, candidate_id: uuid.UUID, corrections: Sequence[Correction]) -> None:
        """Keep a call's record to what was heard (voice.py works out what): our lines cut or removed, in the chat and
        in the model messages of the turn that wrote them, so the history after the call holds only what the
        candidate heard; the candidate's words transcribed again."""
        if not corrections:
            return
        events: list[ChatEvent] = []
        async with self.sessionmaker() as session, session.begin():
            for correction in corrections:
                message = await session.get_one(Message, correction.message_id)
                if correction.heard is None:
                    await session.delete(message)
                else:
                    message.text = correction.heard
                    message.interrupted = message.author is Author.ASSISTANT
                events.append(Corrected(id=message.id, text=correction.heard, interrupted=message.interrupted))
                if message.author is Author.CANDIDATE or message.turn_id is None:
                    continue
                turn = await session.get_one(Turn, message.turn_id)
                # Our text leaves the turn's responses (a response left empty goes too: PydanticAI merges the
                # requests around it); the part heard, if any, ends the turn.
                history: list[ModelMessage] = []
                for model_message in ModelMessagesTypeAdapter.validate_python(turn.messages):
                    if isinstance(model_message, ModelResponse):
                        parts = [part for part in model_message.parts if not isinstance(part, TextPart)]
                        if not parts:
                            continue
                        model_message = dataclasses.replace(model_message, parts=parts)
                    history.append(model_message)
                if correction.heard:
                    history.append(ModelResponse(parts=[TextPart(correction.heard)]))
                turn.messages = ModelMessagesTypeAdapter.dump_python(history, mode="json")
        await self.channel.publish(candidate_id, events)

    async def last_turn_id(self, candidate_id: uuid.UUID) -> int:
        """Where a call's history from our stored turns ends: the turns before it."""
        async with self.sessionmaker() as session:
            last = await session.scalar(select(func.max(Turn.id)).where(Turn.candidate_id == candidate_id))
        return last or 0

    async def _record_turn(
        self, session: AsyncSession, candidate: Candidate, result: TurnResult, now: datetime, started: float
    ) -> Turn:
        """A finished turn: its model messages, and the state it leaves on the candidate."""
        turn = Turn(
            candidate_id=candidate.id,
            messages=ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
            prompt_version=prompt_version(self.config),
            models=[m.model_name or "unknown" for m in result.messages if isinstance(m, ModelResponse)],
            usage=to_jsonable_python(result.usage),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        session.add(turn)
        await session.flush()
        row = await session.get_one(Candidate, candidate.id)
        row.state = result.state.model_dump(mode="json")
        row.status = result.state.status
        row.outcome = result.state.outcome
        if result.reply:
            row.last_outbound_at = now
            # A new silence: the reminders start over.
            row.reminders_sent = 0
        if result.state.status is Status.COMPLETED and candidate.status is not Status.COMPLETED:
            row.completed_at = now
        return turn

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
            if not pending:
                # A pick-up after a call: nothing waits for an answer, so there's nothing to promise.
                return []
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
        self._summarized.add(candidate_id)
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

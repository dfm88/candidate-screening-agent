"""The clock's work through tick(now), and the metrics, against the compose Postgres: reminders and their quiet hours,
*abandoned*, the capped retry and the summaries a restart lost. The clock is injected; the LLM is stubbed. Then the
chat's worker: a run thrown away, and run again, when a message arrives while it goes.

Each test of tick or the metrics starts from empty tables, because they look at every candidate.
"""

import asyncio
import itertools
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from screening.channels import WebChatChannel
from screening.config import DEFAULT_CLIENT_CONFIG, Language, Settings, load_client_config
from screening.contract import Author, ChatEvent, ChatMessage, MessageKind, Tab, Typing
from screening.db import Candidate, Message, Source, Turn, make_sessionmaker
from screening.domain import (
    Answers,
    AreaMatch,
    Availability,
    Experience,
    LicenseAnswer,
    Outcome,
    Schedule,
    ScreeningState,
    Status,
)
from screening.service import ScreeningService

pytestmark = pytest.mark.anyio

CONFIG = load_client_config(DEFAULT_CLIENT_CONFIG)
# Monday 26 October 2026, 09:00 in Madrid (CET since the 25th) and 02:00 in Mexico City.
MORNING = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
NAME_AND_LICENSE = Answers(full_name="Ana Torres Gil", license=LicenseAnswer.YES)


def summary_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output = {"caveats": [], "notes": ["Escrito después del reinicio."]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


@pytest.fixture
async def service(database_url: str) -> AsyncIterator[ScreeningService]:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE candidates CASCADE"))
    service = ScreeningService(
        sessionmaker=make_sessionmaker(engine),
        config=CONFIG,
        # Long enough that a worker tick starts never reaches the model: these tests are about what tick starts.
        settings=Settings(chat_debounce_seconds=60),
        model=summary_model(),
        channel=WebChatChannel(),
    )
    yield service
    await service.stop()
    await engine.dispose()


async def applied(
    service: ScreeningService,
    vacancy: str = "es-mad-chamberi",
    status: Status = Status.INVITED,
    answers: Answers | None = None,
    **columns: object,
) -> uuid.UUID:
    """An application through the service (first message included), then moved to where the test starts: silent since
    MORNING, unless `columns` says otherwise."""
    candidate = await service.apply(
        external_id=f"test:{uuid.uuid4()}",
        source=Source.APPLY_FORM,
        full_name="Ana Torres Gil",
        phone="+34 600 000 000",
        vacancy_location_id=vacancy,
        language=Language.ES,
        whatsapp_opt_in=False,
    )
    state = ScreeningState(status=status, language=Language.ES, answers=answers or Answers())
    values = {"status": status, "state": state.model_dump(mode="json"), "last_outbound_at": MORNING} | columns
    async with service.sessionmaker() as session, session.begin():
        await session.execute(update(Candidate).where(Candidate.id == candidate.id).values(values))
    return candidate.id


async def load(service: ScreeningService, candidate_id: uuid.UUID) -> Candidate:
    async with service.sessionmaker() as session:
        return await session.get_one(Candidate, candidate_id)


async def templates(service: ScreeningService, candidate_id: uuid.UUID) -> list[tuple[str | None, str]]:
    """The messages we sent from a template, after the first one."""
    async with service.sessionmaker() as session:
        rows = await session.scalars(
            select(Message).where(Message.candidate_id == candidate_id).order_by(Message.id).offset(1)
        )
        return [(row.template_key, row.text) for row in rows]


async def test_two_reminders_then_abandoned(service: ScreeningService) -> None:
    candidate = await applied(service)

    assert (await service.tick(MORNING + timedelta(hours=3, minutes=59))).reminded == []
    assert (await service.tick(MORNING + timedelta(hours=4))).reminded == [candidate]
    assert (await service.tick(MORNING + timedelta(hours=5))).reminded == []
    assert (await service.tick(MORNING + timedelta(days=1))).reminded == [candidate]
    assert (await service.tick(MORNING + timedelta(days=2))).reminded == []
    reminder = "Hola, Ana. ¿Seguimos con tu solicitud para repartir en Madrid? Son solo 3 minutos."
    assert await templates(service, candidate) == [("reminder_not_started", reminder)] * 2

    # Stored as turns, like every message we send: the agent sees them in its history.
    async with service.sessionmaker() as session:
        turns = (await session.scalars(select(Turn).where(Turn.candidate_id == candidate))).all()
    assert [turn.prompt_version for turn in turns] == [None, None, None]

    ended = await service.tick(MORNING + timedelta(days=3))
    assert (ended.abandoned, ended.reminded) == ([candidate], [])
    row = await load(service, candidate)
    assert row.status is Status.ABANDONED and row.state["status"] == "abandoned"
    assert (await service.tick(MORNING + timedelta(days=4))) == type(ended)()


async def test_a_late_tick_sends_one_reminder_for_those_it_missed(service: ScreeningService) -> None:
    candidate = await applied(service)

    assert (await service.tick(MORNING + timedelta(days=1, hours=1))).reminded == [candidate]
    assert (await load(service, candidate)).reminders_sent == 2
    assert (await service.tick(MORNING + timedelta(days=2))).reminded == []


async def test_mid_chat_reminders_name_the_stage_and_wait_out_local_quiet_hours(service: ScreeningService) -> None:
    # 19:30 in Madrid, 12:30 in Mexico City.
    evening = datetime(2026, 10, 26, 18, 30, tzinfo=UTC)
    everything = NAME_AND_LICENSE.model_copy(
        update={
            "area": AreaMatch(location_id="es-mad-chamberi", postal_code="28010"),
            "availability": [Availability.FULL_TIME],
            "schedule": [Schedule.EVENING],
            "experience": Experience(years=0),
            "start_date": datetime(2026, 11, 2).date(),
        }
    )
    at_the_recap = await applied(service, status=Status.IN_PROGRESS, answers=everything, last_outbound_at=evening)
    at_the_area = await applied(
        service, "mx-cdmx-roma", Status.IN_PROGRESS, answers=NAME_AND_LICENSE, last_outbound_at=evening
    )

    # Two hours later: 21:30 in Madrid, quiet; 14:30 in Mexico City.
    assert (await service.tick(evening + timedelta(hours=2))).reminded == [at_the_area]
    assert await templates(service, at_the_area) == [
        ("reminder_in_progress", "Hola, Ana. Nos quedamos en tu zona. ¿Seguimos?")
    ]
    # 09:00 the next day in Madrid.
    assert (await service.tick(datetime(2026, 10, 27, 8, 0, tzinfo=UTC))).reminded == [at_the_recap]
    assert await templates(service, at_the_recap) == [
        ("reminder_in_progress", "Hola, Ana. Nos quedamos en el resumen de tus respuestas. ¿Seguimos?")
    ]


async def test_the_timed_retry_stops_after_three_failed_turns(service: ScreeningService) -> None:
    long_silent: dict[str, Any] = {"last_outbound_at": MORNING - timedelta(days=4)}
    failing = await applied(service, status=Status.IN_PROGRESS, **long_silent)
    waiting = await applied(service, status=Status.IN_PROGRESS, **long_silent)
    # Its failed turns came before the message now pending: they count for nothing.
    failed_before = await applied(service, status=Status.IN_PROGRESS, **long_silent)
    async with service.sessionmaker() as session, session.begin():
        for candidate, sent in ((failing, MORNING), (waiting, MORNING - timedelta(hours=3)), (failed_before, MORNING)):
            session.add(
                Message(
                    candidate_id=candidate,
                    author=Author.CANDIDATE,
                    kind=MessageKind.TEXT,
                    text="hola",
                    client_id="m1",
                    created_at=sent,
                )
            )
        for candidate, failed in [(failing, MORNING + timedelta(seconds=30))] * 2 + [
            (failed_before, MORNING - timedelta(hours=2))
        ] * 3:
            session.add(Turn(candidate_id=candidate, messages=[], error="RuntimeError()", created_at=failed))

    retry = MORNING + timedelta(minutes=2)
    done = await service.tick(retry)
    assert set(done.retried) == {failing, waiting, failed_before}
    # Someone wrote: no reminder, and not abandoned however long the silence before.
    assert (done.reminded, done.abandoned) == ([], [])

    # The workers it started never reach the model here; a third failed turn is recorded as one would.
    await service.stop()
    async with service.sessionmaker() as session, session.begin():
        session.add(Turn(candidate_id=failing, messages=[], error="RuntimeError()", created_at=retry))
    assert set((await service.tick(retry + timedelta(seconds=30))).retried) == {waiting, failed_before}

    found = await service.candidate(failing)
    assert found is not None
    _, tab, replies_failed, _ = found
    assert (tab, replies_failed) == (Tab.REVIEW, True)


async def test_tick_writes_the_summaries_a_restart_lost(service: ScreeningService) -> None:
    finished = await applied(service, status=Status.COMPLETED, answers=NAME_AND_LICENSE, outcome=Outcome.QUALIFIED)
    withdrawn = await applied(service, status=Status.WITHDRAWN)

    assert set((await service.tick(MORNING)).summarized) == {finished, withdrawn}
    assert (await load(service, finished)).summary == {"caveats": [], "notes": ["Escrito después del reinicio."]}
    assert (await service.tick(MORNING)).summarized == []


async def test_metrics(service: ScreeningService) -> None:
    replied: dict[str, Any] = {"first_reply_at": MORNING, "created_at": MORNING}

    def finished(minutes: int, outcome: Outcome, decided: Outcome | None = None) -> dict[str, Any]:
        return replied | {
            "outcome": outcome,
            "decided_outcome": decided,
            "completed_at": MORNING + timedelta(minutes=minutes),
        }

    await applied(service)
    await applied(service, status=Status.ABANDONED)
    await applied(service, status=Status.ABANDONED, answers=NAME_AND_LICENSE, **replied)
    await applied(service, status=Status.IN_PROGRESS, **replied)
    # The rules said qualified and the recruiter didn't agree: an override.
    await applied(service, status=Status.COMPLETED, **finished(10, Outcome.QUALIFIED, Outcome.NOT_QUALIFIED))
    await applied(service, status=Status.COMPLETED, **finished(20, Outcome.NOT_QUALIFIED, Outcome.NOT_QUALIFIED))
    # Not opened by a recruiter: it says nothing about how often they disagree.
    await applied(service, status=Status.COMPLETED, **finished(20, Outcome.NOT_QUALIFIED))
    # A decided review case is the review working, not an override.
    await applied(service, status=Status.COMPLETED, **finished(30, Outcome.NEEDS_HUMAN_REVIEW, Outcome.QUALIFIED))

    metrics = await service.metrics()
    assert metrics.model_dump() == {
        "applied": 8,
        "response_rate": 6 / 8,
        "completion_rate": 4 / 6,
        "drop_off": {"not_started": 1, "service_area": 1},
        "review_rate": 1 / 4,
        "overrides": 1,
        "override_rate": 1 / 2,
        "median_seconds_to_outcome": 20 * 60,
    }


@dataclass
class HeldModel:
    """The screening model, holding each run at the model until the test lets one through. `runs` gets the candidate's
    messages each run answers, as the run reaches the model. Every reply costs the same tokens."""

    runs: asyncio.Queue[list[str]] = field(default_factory=asyncio.Queue)
    gate: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(0))
    failing: bool = False

    async def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = messages[-1].parts[-1]
        assert isinstance(prompt, UserPromptPart)
        self.runs.put_nowait(re.findall(r"<message>(.*?)</message>", str(prompt.content)))
        await self.gate.acquire()
        if self.failing:
            raise RuntimeError("the model is down")
        return ModelResponse(
            parts=[TextPart("Gracias, Marta. ¿Tienes carnet de conducir en vigor?")],
            usage=RequestUsage(input_tokens=1000, output_tokens=20),
        )

    def let_through(self) -> None:
        self.gate.release()

    async def next_run(self) -> list[str]:
        async with asyncio.timeout(5):
            return await self.runs.get()


@pytest.fixture
def model() -> HeldModel:
    return HeldModel()


@pytest.fixture
def channel() -> WebChatChannel:
    return WebChatChannel()


@pytest.fixture
async def answering(database_url: str, model: HeldModel, channel: WebChatChannel) -> AsyncIterator[ScreeningService]:
    """A service whose worker answers on the held model, after a short debounce."""
    engine = create_async_engine(database_url)
    service = ScreeningService(
        sessionmaker=make_sessionmaker(engine),
        config=CONFIG,
        settings=Settings(chat_debounce_seconds=0.05),
        model=FunctionModel(model.respond),
        channel=channel,
    )
    yield service
    await service.stop()
    await engine.dispose()


async def until_answered(events: asyncio.Queue[ChatEvent]) -> list[ChatEvent]:
    """What the chat shows up to the typing indicator going off, which ends a turn."""
    seen: list[ChatEvent] = []
    async with asyncio.timeout(5):
        while not (seen and seen[-1] == Typing(active=False)):
            seen.append(await events.get())
    return seen


def replies(seen: list[ChatEvent]) -> list[str]:
    return [event.text for event in seen if isinstance(event, ChatMessage) and event.author is Author.ASSISTANT]


async def answered(service: ScreeningService, candidate_id: uuid.UUID) -> list[tuple[int | None, list[str]]]:
    """The candidate's messages, grouped by the turn that answered them (None while they wait)."""
    async with service.sessionmaker() as session:
        rows = await session.scalars(
            select(Message)
            .where(Message.candidate_id == candidate_id, Message.author == Author.CANDIDATE)
            .order_by(Message.id)
        )
        return [
            (turn_id, [row.text for row in group])
            for turn_id, group in itertools.groupby(rows, lambda row: row.turn_id)
        ]


async def a_run_thrown_away(
    service: ScreeningService, model: HeldModel, channel: WebChatChannel, candidate: Candidate
) -> list[ChatEvent]:
    """The candidate writes "hola", then "soy Marta" while the run on "hola" goes: that run ends and is thrown away, and
    the next one answers both. What the chat shows until then."""
    with channel.subscribe(candidate.id) as events:
        await service.receive(candidate, "hola", "m1")
        assert await model.next_run() == ["hola"]
        await service.receive(candidate, "soy Marta", "m2")
        model.let_through()
        assert await model.next_run() == ["hola", "soy Marta"]
        model.let_through()
        return await until_answered(events)


async def test_a_message_during_a_turn_gets_one_reply_with_the_one_before(
    answering: ScreeningService, model: HeldModel, channel: WebChatChannel
) -> None:
    candidate = await load(answering, await applied(answering, status=Status.IN_PROGRESS))

    seen = await a_run_thrown_away(answering, model, channel, candidate)

    assert replies(seen) == ["Gracias, Marta. ¿Tienes carnet de conducir en vigor?"]
    # On from the first run to the reply.
    assert [event for event in seen if isinstance(event, Typing)] == [Typing(active=True)] * 2 + [Typing(active=False)]
    assert [texts for _, texts in await answered(answering, candidate.id)] == [["hola", "soy Marta"]]


async def test_a_message_while_the_turn_is_stored_gets_the_next_turn(
    answering: ScreeningService, model: HeldModel, channel: WebChatChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = await load(answering, await applied(answering, status=Status.IN_PROGRESS))
    storing, stored = asyncio.Event(), asyncio.Event()
    record_turn = answering._record_turn

    async def held_record_turn(*args: Any) -> Turn:
        storing.set()
        await stored.wait()
        return await record_turn(*args)

    monkeypatch.setattr(answering, "_record_turn", held_record_turn)

    with channel.subscribe(candidate.id) as events:
        await answering.receive(candidate, "hola", "m1")
        assert await model.next_run() == ["hola"]
        model.let_through()
        async with asyncio.timeout(5):
            await storing.wait()
        await answering.receive(candidate, "soy Marta", "m2")
        stored.set()
        first = await until_answered(events)
        assert await model.next_run() == ["soy Marta"]
        model.let_through()
        second = await until_answered(events)

    assert (len(replies(first)), len(replies(second))) == (1, 1)
    assert [texts for _, texts in await answered(answering, candidate.id)] == [["hola"], ["soy Marta"]]


async def test_after_two_runs_thrown_away_the_third_is_sent_and_the_count_starts_over(
    answering: ScreeningService, model: HeldModel, channel: WebChatChannel
) -> None:
    candidate = await load(answering, await applied(answering, status=Status.IN_PROGRESS))

    with channel.subscribe(candidate.id) as events:
        await answering.receive(candidate, "hola", "m1")
        assert await model.next_run() == ["hola"]
        await answering.receive(candidate, "soy Marta", "m2")
        model.let_through()
        assert await model.next_run() == ["hola", "soy Marta"]
        await answering.receive(candidate, "vivo en el 28015", "m3")
        model.let_through()
        assert await model.next_run() == ["hola", "soy Marta", "vivo en el 28015"]
        # Two runs thrown away already: this one is sent, and the new message gets the next turn.
        await answering.receive(candidate, "tengo moto", "m4")
        model.let_through()
        first = await until_answered(events)
        assert await model.next_run() == ["tengo moto"]
        # A turn was stored: a message during the next run throws it away again.
        await answering.receive(candidate, "y carnet", "m5")
        model.let_through()
        assert await model.next_run() == ["tengo moto", "y carnet"]
        model.let_through()
        second = await until_answered(events)

    assert (len(replies(first)), len(replies(second))) == (1, 1)
    assert [texts for _, texts in await answered(answering, candidate.id)] == [
        ["hola", "soy Marta", "vivo en el 28015"],
        ["tengo moto", "y carnet"],
    ]


async def test_a_run_thrown_away_is_not_a_failed_turn(
    answering: ScreeningService, model: HeldModel, channel: WebChatChannel
) -> None:
    candidate = await load(answering, await applied(answering, status=Status.IN_PROGRESS))

    await a_run_thrown_away(answering, model, channel, candidate)

    # No failed turn for tick()'s retry cap, and no "give me a moment".
    async with answering.sessionmaker() as session:
        errors = await session.scalars(select(Turn.error).where(Turn.candidate_id == candidate.id))
        notices = await session.scalars(
            select(Message.id).where(Message.candidate_id == candidate.id, Message.template_key == "failure")
        )
        assert (errors.all(), notices.all()) == ([None, None], [])


async def test_the_stored_turn_counts_the_usage_of_the_run_thrown_away(
    answering: ScreeningService, model: HeldModel, channel: WebChatChannel
) -> None:
    candidate = await load(answering, await applied(answering, status=Status.IN_PROGRESS))

    await a_run_thrown_away(answering, model, channel, candidate)

    async with answering.sessionmaker() as session:
        usage = await session.scalar(
            select(Turn.usage).where(Turn.candidate_id == candidate.id, Turn.prompt_version.is_not(None))
        )
    assert usage is not None
    assert (usage["requests"], usage["input_tokens"], usage["output_tokens"]) == (2, 2000, 40)


async def test_a_failed_run_is_a_failure_even_if_a_message_came_in_while_it_ran(
    answering: ScreeningService, model: HeldModel, channel: WebChatChannel
) -> None:
    candidate = await load(answering, await applied(answering, status=Status.IN_PROGRESS))

    with channel.subscribe(candidate.id) as events:
        await answering.receive(candidate, "hola", "m1")
        assert await model.next_run() == ["hola"]
        await answering.receive(candidate, "soy Marta", "m2")
        model.failing = True
        model.let_through()
        seen = await until_answered(events)
        # Longer than the debounce: a run again would have started by now.
        await asyncio.sleep(0.3)

    assert replies(seen) == ["Dame un momento, por favor."]
    assert model.runs.empty()
    assert await answered(answering, candidate.id) == [(None, ["hola", "soy Marta"])]

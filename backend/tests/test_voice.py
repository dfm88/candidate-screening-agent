"""A live voice call through `Call` (voice.py) and the service, against the compose Postgres, with the model stubbed
and ElevenLabs' transcripts scripted: which transcripts run a turn, what a cancelled turn leaves, and how the stored
record follows what the candidate heard.

A transcript is the whole call: "user" lines as transcribed, "agent" lines cut to what ElevenLabs played.
"""

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

import pytest
from elevenlabs import AsyncElevenLabs
from elevenlabs.speech_engine import ConversationMessage, SpeechEngineSession
from logfire.testing import CaptureLogfire
from pydantic_ai import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from screening.channels import WebChatChannel
from screening.config import DEFAULT_CLIENT_CONFIG, Language, Settings, load_client_config
from screening.contract import ChatMessage, MessageKind
from screening.db import Message, Source, Turn, make_sessionmaker
from screening.service import ScreeningService
from screening.voice import Call

pytestmark = pytest.mark.anyio

CONFIG = load_client_config(DEFAULT_CLIENT_CONFIG)
START = "Sí, empezamos."
ASK_NAME = "Soy un asistente virtual. ¿Tu nombre completo es Ana Torres Gil?"
NAME = "Sí, así es."
ASK_LICENSE = "Vale. ¿Tienes carnet de conducir en vigor?"


@dataclass
class Model:
    """Answers each thing the candidate says with a fixed reply, the first word streamed on its own. A reply to what's
    in `hold` waits for `gate`: after its first word when streamed (a call), before it's sent when whole (the chat).
    A turn with no new messages (the chat picking up after a call) is answered under ""."""

    replies: dict[str, str]
    # In the chat, replies to these call a tool first: its name and arguments.
    tools: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    # What the second request writes when the reply asked nothing (agent.leaves_something_to_answer).
    continuations: dict[str, str] = field(default_factory=dict)
    hold: set[str] = field(default_factory=set)
    # Replies to these fail, as when every model is down.
    fail: set[str] = field(default_factory=set)
    sent: list[list[ModelMessage]] = field(default_factory=list)
    called: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    running: int = 0
    most_running: int = 0

    def reply_to(self, messages: list[ModelMessage]) -> tuple[str, str]:
        self.sent.append(messages)
        self.called.set()
        prompts = [
            str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)
        ]
        # The turn's own prompt is the last with the candidate's words; a retry's feedback comes as a prompt after it.
        prompt = next(prompt for prompt in reversed(prompts) if "<new_messages>" in prompt)
        said = (re.findall(r"<message>(.*?)</message>", prompt) or [""])[-1]
        if "<retry_feedback>" in prompts[-1]:
            return said, self.continuations[said]
        return said, self.replies[said]

    async def stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            said, reply = self.reply_to(messages)
            if said in self.fail:
                raise RuntimeError("every model is down")
            first, rest = reply.split(" ", 1)
            yield f"{first} "
            if said in self.hold:
                await self.gate.wait()
            yield rest
        finally:
            self.running -= 1

    async def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            said, reply = self.reply_to(messages)
            if said in self.tools and not isinstance(messages[-1].parts[-1], ToolReturnPart):
                name, arguments = self.tools[said]
                return ModelResponse(parts=[ToolCallPart(name, arguments)])
            if said in self.hold:
                await self.gate.wait()
            return ModelResponse(parts=[TextPart(reply)])
        finally:
            self.running -= 1


@dataclass
class ElevenLabs:
    """ElevenLabs' side of `send_response`: it takes our reply as it streams, or whole."""

    spoken: list[str] = field(default_factory=list)
    first_chunk: asyncio.Event = field(default_factory=asyncio.Event)
    # Sending hangs after the first chunk, so a cancel lands in the SDK's code rather than in our turn's.
    stall: bool = False

    async def send_response(self, reply: AsyncIterator[str] | str) -> None:
        if isinstance(reply, str):
            self.spoken.append(reply)
            return
        chunks = []
        async for chunk in reply:
            chunks.append(chunk)
            self.first_chunk.set()
            if self.stall:
                await asyncio.Event().wait()
        self.spoken.append("".join(chunks).strip())


def transcript(*lines: tuple[str, str]) -> list[ConversationMessage]:
    return [ConversationMessage(role=role, content=content) for role, content in lines]


@pytest.fixture
async def database(database_url: str) -> AsyncIterator[str]:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE candidates CASCADE"))
    await engine.dispose()
    yield database_url


@pytest.fixture
async def services() -> AsyncIterator[list[ScreeningService]]:
    """Every service a test starts, stopped at the end."""
    started: list[ScreeningService] = []
    yield started
    for service in started:
        await service.stop()


async def call_on(
    services: list[ScreeningService], database: str, model: Model
) -> tuple[ScreeningService, uuid.UUID, Call]:
    """A service on the stubbed model, an application through it (the first message included), and a call."""
    service = ScreeningService(
        sessionmaker=make_sessionmaker(create_async_engine(database)),
        config=CONFIG,
        settings=Settings(chat_debounce_seconds=0.01),
        model=FunctionModel(model.respond, stream_function=model.stream),
        channel=WebChatChannel(),
    )
    services.append(service)
    candidate = await service.apply(
        external_id=f"test:{uuid.uuid4()}",
        source=Source.APPLY_FORM,
        full_name="Ana Torres Gil",
        phone="+34 600 000 000",
        vacancy_location_id="es-mad-chamberi",
        language=Language.ES,
        whatsapp_opt_in=False,
    )
    call = Call(service, candidate.id, call_id="call", history_until=await service.last_turn_id(candidate.id))
    return service, candidate.id, call


async def chat(service: ScreeningService, candidate_id: uuid.UUID) -> list[tuple[str, str, bool]]:
    """The conversation as stored after the first message: author, text, and whether our line was cut off."""
    async with service.sessionmaker() as session:
        rows = await session.scalars(
            select(Message).where(Message.candidate_id == candidate_id, Message.kind != MessageKind.TEMPLATE)
        )
        return [(row.author, row.text, row.interrupted) for row in sorted(rows, key=lambda row: row.id)]


async def turns(service: ScreeningService, candidate_id: uuid.UUID) -> list[list[ModelMessage]]:
    async with service.sessionmaker() as session:
        stored = await session.scalars(select(Turn).where(Turn.candidate_id == candidate_id).order_by(Turn.id))
        return [ModelMessagesTypeAdapter.validate_python(turn.messages) for turn in stored]


def texts(messages: list[ModelMessage]) -> list[str]:
    return [part.content for message in messages for part in message.parts if isinstance(part, TextPart)]


async def test_a_transcript_sent_again_runs_no_turn_and_gets_our_reply_again(
    services: list[ScreeningService], database: str
) -> None:
    model = Model({START: ASK_NAME, NAME: ASK_LICENSE})
    service, candidate, call = await call_on(services, database, model)
    elevenlabs = ElevenLabs()
    first = transcript(("user", START))
    second = transcript(("user", START), ("agent", ASK_NAME), ("user", NAME))

    await call.answer(first, elevenlabs.send_response, "conv_1")
    # Seen live: the same words again, punctuated anew, after our turn was stored. ElevenLabs dropped the reply when
    # it sent them, and waits for one to the new transcript: the same reply, whole.
    await call.answer(transcript(("user", START.lower().rstrip("."))), elevenlabs.send_response, "conv_1")
    await call.answer(second, elevenlabs.send_response, "conv_1")
    # The older transcript sent again, after the newer one: it stands for the newer, whose reply hasn't been played.
    await call.answer(first, elevenlabs.send_response, "conv_1")
    # Seen live: the older one back again with a line of ours at its end, what it got of a reply cut off. It asks
    # for nothing, and gets nothing: saying our last reply again would say it twice.
    await call.answer(transcript(("user", START), ("agent", "Soy un asistente")), elevenlabs.send_response, "conv_1")
    # Sent again once our reply was played: nothing to say.
    played = transcript(("user", START), ("agent", ASK_NAME), ("user", NAME), ("agent", ASK_LICENSE), ("user", "…"))
    await call.answer(played, elevenlabs.send_response, "conv_1")

    # A turn on any of them would read the candidate's old words against our last question.
    assert len(model.sent) == 2
    assert elevenlabs.spoken == [ASK_NAME, ASK_NAME, ASK_LICENSE, ASK_LICENSE, "", ""]
    assert await chat(service, candidate) == [
        ("candidate", START, False),
        ("assistant", ASK_NAME, False),
        ("candidate", NAME, False),
        ("assistant", ASK_LICENSE, False),
    ]


async def test_an_older_transcript_with_other_words_runs_no_turn_by_its_event_id(
    services: list[ScreeningService], database: str
) -> None:
    # Seen live: ElevenLabs read the candidate's words twice ("Diego Riconi", then "Diego Rigoni"), the second
    # cutting our turn on the first, and once that was answered it sent the first reading again. Its words don't
    # begin the newer one's, so only its event id tells it's older.
    first_reading = "Sí, a sí es."
    model = Model({START: ASK_NAME, first_reading: ASK_LICENSE, NAME: ASK_LICENSE}, hold={first_reading})
    service, candidate, call = await call_on(services, database, model)
    elevenlabs = ElevenLabs()
    asked = (("user", START), ("agent", ASK_NAME))

    await call.answer(transcript(("user", START)), elevenlabs.send_response, "conv_1", 1)
    model.called.clear()
    answering = asyncio.create_task(
        call.answer(transcript(*asked, ("user", first_reading)), elevenlabs.send_response, "conv_1", 2)
    )
    await model.called.wait()
    answering.cancel()
    await asyncio.gather(answering, return_exceptions=True)
    await call.answer(transcript(*asked, ("user", NAME)), elevenlabs.send_response, "conv_1", 3)
    # A turn on it would read the first reading against our last question, and wait on the held reply.
    await asyncio.wait_for(
        call.answer(transcript(*asked, ("user", first_reading)), elevenlabs.send_response, "conv_1", 2), timeout=5
    )

    assert len(model.sent) == 3
    # The transcript as sent shows none of our last reply played: it's said again.
    assert elevenlabs.spoken == [ASK_NAME, ASK_LICENSE, ASK_LICENSE]
    assert await chat(service, candidate) == [
        ("candidate", START, False),
        ("assistant", ASK_NAME, False),
        ("candidate", NAME, False),
        ("assistant", ASK_LICENSE, False),
    ]


async def test_a_transcript_handler_reads_its_own_event_id_from_the_sdk() -> None:
    """voice.py reads the SDK's private `_current_event_id` in the handler, the one way to an event's id: this fails
    if the SDK drops it, or if a handler no longer reads its own transcript's id there."""
    received = [
        json.dumps(
            {"type": "user_transcript", "event_id": event_id, "user_transcript": [{"role": "user", "content": "Hola"}]}
        )
        for event_id in (7, 8)
    ] + [json.dumps({"type": "close"})]

    class Socket:
        async def recv(self) -> str:
            return received.pop(0)

        async def send(self, data: str) -> None:
            pass

    session = SpeechEngineSession(Socket())
    read: list[int | None] = []
    session.on("user_transcript", lambda transcript: read.append(session._current_event_id))
    await asyncio.wait_for(session.run(), timeout=5)

    assert read == [7, 8]


async def test_a_turn_in_a_call_that_asks_nothing_goes_on_after_what_was_spoken_once(
    services: list[ScreeningService], database: str
) -> None:
    got_it = "Vale, Ana."
    model = Model({START: ASK_NAME, NAME: got_it}, continuations={NAME: ASK_LICENSE})
    service, candidate, call = await call_on(services, database, model)
    elevenlabs = ElevenLabs()
    lines = [("user", START), ("agent", ASK_NAME), ("user", NAME)]

    await call.answer(transcript(*lines[:1]), elevenlabs.send_response, "conv_1")
    await call.answer(transcript(*lines), elevenlabs.send_response, "conv_1")
    # Seen live: "Got it, afternoons." and nothing more. What was spoken stays, and a second request goes on from it.
    assert " ".join(elevenlabs.spoken[-1].split()) == f"{got_it} {ASK_LICENSE}"

    # Once: the second reply stands even when it asks nothing, a deliberate wait.
    wait = "Espera un momento."
    model.replies[wait], model.continuations[wait] = "Claro, Ana.", "Te escucho."
    requests = len(model.sent)
    await call.answer(
        transcript(*lines, ("agent", f"{got_it} {ASK_LICENSE}"), ("user", wait)), elevenlabs.send_response, "conv_1"
    )
    assert (len(model.sent) - requests, elevenlabs.spoken[-1]) == (2, "Claro, Ana. Te escucho.")


async def test_a_call_ending_on_a_line_that_asks_nothing_is_picked_up_in_the_chat(
    services: list[ScreeningService], database: str
) -> None:
    wait = "Espera, que me llaman."
    pick_up = "Seguimos por aquí cuando puedas. ¿Tu nombre completo es Ana Torres Gil?"
    model = Model({START: ASK_NAME, wait: "Claro, Ana.", "": pick_up}, continuations={wait: "Te espero."})
    service, candidate, call = await call_on(services, database, model)
    elevenlabs = ElevenLabs()
    await call.answer(transcript(("user", START)), elevenlabs.send_response, "conv_1")
    await call.answer(
        transcript(("user", START), ("agent", ASK_NAME), ("user", wait)), elevenlabs.send_response, "conv_1"
    )

    # They hang up: our last line asks nothing, and the screening is open. No ElevenLabs history: the call is kept as
    # stored.
    assert isinstance(service.channel, WebChatChannel)
    with service.channel.subscribe(candidate) as events:
        await call.end(None, cast(AsyncElevenLabs, None))
        while True:
            event = await asyncio.wait_for(events.get(), timeout=5)
            if isinstance(event, ChatMessage) and event.text == pick_up:
                break
    assert (await chat(service, candidate))[-2:] == [
        ("assistant", "Claro, Ana. Te espero.", False),
        ("assistant", pick_up, False),
    ]


async def test_a_cut_reply_is_stored_as_heard_and_an_unplayed_one_is_removed(
    services: list[ScreeningService], database: str
) -> None:
    pardon = "Perdona, no te he oído."
    pardon_again = "Perdona, no te he oído. ¿Qué?"
    repeat = "Te preguntaba tu nombre. ¿Es Ana Torres Gil?"
    model = Model({START: ASK_NAME, pardon: repeat, pardon_again: repeat})
    service, candidate, call = await call_on(services, database, model)
    elevenlabs = ElevenLabs()

    await call.answer(transcript(("user", START)), elevenlabs.send_response, "conv_1")
    # The candidate cut in: only the start of our stored reply was played.
    played = "Soy un asistente virtual."
    await call.answer(
        transcript(("user", START), ("agent", played), ("user", pardon)), elevenlabs.send_response, "conv_1"
    )

    # The model read our line as played (after the first message), never the question the candidate didn't hear.
    assert texts(model.sent[1])[1:] == [played]
    # Stored as heard, in the chat and in the turn's model messages, which later turns read.
    assert await chat(service, candidate) == [
        ("candidate", START, False),
        ("assistant", played, True),
        ("candidate", pardon, False),
        ("assistant", repeat, False),
    ]
    assert texts((await turns(service, candidate))[1]) == [played]

    # They spoke on before our reply was played: ElevenLabs' line for them grew, and no line of ours came between.
    await call.answer(
        transcript(("user", START), ("agent", played), ("user", pardon_again)), elevenlabs.send_response, "conv_1"
    )
    assert await chat(service, candidate) == [
        ("candidate", START, False),
        ("assistant", played, True),
        ("candidate", pardon_again, False),
        ("assistant", repeat, False),
    ]
    assert texts((await turns(service, candidate))[2]) == []

    # Cut before a word of it was played: ElevenLabs' line for it is "...", and nothing of it was heard.
    hello = "¿Hola?"
    model.replies[hello] = "Sí, aquí estoy. ¿Es Ana Torres Gil tu nombre completo?"
    lines = [("user", START), ("agent", played), ("user", pardon_again), ("agent", "..."), ("user", hello)]
    await call.answer(transcript(*lines), elevenlabs.send_response, "conv_1")
    assert [line for line in await chat(service, candidate) if line[1] in (repeat, "...")] == []


@pytest.mark.parametrize("cut_while", ["writing", "sending"])
async def test_an_interrupted_turn_stores_nothing_even_after_speaking_and_the_next_does_not_wait(
    services: list[ScreeningService], database: str, capfire: CaptureLogfire, cut_while: str
) -> None:
    question = "Espera, una pregunta."
    answer = "Claro, ¿qué quieres saber?"
    model = Model({START: ASK_NAME, question: answer}, hold={START})
    service, candidate, call = await call_on(services, database, model)
    speaking = ElevenLabs(stall=cut_while == "sending")
    elevenlabs = ElevenLabs()

    answering = asyncio.create_task(call.answer(transcript(("user", START)), speaking.send_response, "conv_1"))
    await speaking.first_chunk.wait()
    # The candidate speaks over our first word: ElevenLabs sends a newer transcript, and the SDK cancels the handler.
    answering.cancel()
    await asyncio.gather(answering, return_exceptions=True)

    # The run was cancelled mid-reply (its model stream is gone, though its gate is still closed), and nothing of the
    # turn was stored, not even the candidate's words.
    assert model.running == 0
    assert await chat(service, candidate) == []
    assert len(await turns(service, candidate)) == 1  # the first message

    # The next turn starts at once; the transcript has what was heard of the cut one, which goes in the record.
    newer = transcript(("user", START), ("agent", "Soy..."), ("user", question))
    await asyncio.wait_for(call.answer(newer, elevenlabs.send_response, "conv_1"), timeout=5)
    assert await chat(service, candidate) == [
        ("candidate", START, False),
        ("assistant", "Soy...", True),
        ("candidate", question, False),
        ("assistant", answer, False),
    ]
    # In the model history too, before the turn that answered: the call as the model read it.
    stored = await turns(service, candidate)
    assert [texts(turn) for turn in stored[1:]] == [["Soy..."], [answer]]
    # Cut off is how a call goes, not an error: the turn's span records no exception, and says it was cut off.
    assert [
        (span["attributes"]["logfire.msg"].endswith(": cut off"), "events" in span)
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "screening turn {conversation_id}"
    ] == [(True, False), (False, False)]


async def test_a_failed_turn_in_a_call_says_give_me_a_moment(services: list[ScreeningService], database: str) -> None:
    model = Model({START: ASK_NAME, NAME: ASK_LICENSE}, fail={START})
    service, candidate, call = await call_on(services, database, model)
    elevenlabs = ElevenLabs()
    notice = "Dame un momento, por favor."

    await call.answer(transcript(("user", START)), elevenlabs.send_response, "conv_1")
    assert elevenlabs.spoken == [notice]
    # Only the failure is stored, as in the chat.
    assert await chat(service, candidate) == []
    async with service.sessionmaker() as session:
        errors = await session.scalars(
            select(Turn.error).where(Turn.candidate_id == candidate, Turn.error.is_not(None))
        )
        assert len(errors.all()) == 1

    # The candidate's next words get a turn that answers both, and the line goes in the record as heard.
    await call.answer(
        transcript(("user", START), ("agent", notice), ("user", NAME)), elevenlabs.send_response, "conv_1"
    )
    assert await chat(service, candidate) == [
        ("candidate", START, False),
        ("assistant", notice, False),
        ("candidate", NAME, False),
        ("assistant", ASK_LICENSE, False),
    ]


async def test_after_the_call_the_record_matches_what_was_heard(
    services: list[ScreeningService], database: str
) -> None:
    stop = "No me interesa."
    goodbye = "Entendido, no te escribiremos más. Gracias por tu tiempo."
    model = Model(
        {START: ASK_NAME, NAME: ASK_LICENSE, stop: goodbye},
        hold={stop},
        tools={stop: ("end_conversation", {"reason": "not_interested"})},
    )
    service, candidate, call = await call_on(services, database, model)
    elevenlabs = ElevenLabs()

    await call.answer(transcript(("user", START)), elevenlabs.send_response, "conv_1")
    await call.answer(
        transcript(("user", START), ("agent", ASK_NAME), ("user", NAME)), elevenlabs.send_response, "conv_1"
    )
    # They cut in on the license question to say they're not interested, and hang up as our reply starts.
    cut = "Vale. ¿Tienes carnet…"
    last = transcript(("user", START), ("agent", ASK_NAME), ("user", NAME), ("agent", cut), ("user", stop))
    hanging_up = ElevenLabs()
    answering = asyncio.create_task(call.answer(last, hanging_up.send_response, "conv_1"))
    await hanging_up.first_chunk.wait()
    answering.cancel()
    await asyncio.gather(answering, return_exceptions=True)
    model.gate.set()

    # ElevenLabs' history: none of our last reply was played.
    history = SimpleNamespace(
        status="done",
        transcript=[
            SimpleNamespace(role=role, message=content)
            for role, content in [("user", START), ("agent", ASK_NAME), ("user", NAME), ("agent", cut), ("user", stop)]
        ],
    )
    deleted: list[str] = []

    async def get(conversation_id: str) -> SimpleNamespace:
        return history

    async def delete(conversation_id: str) -> None:
        deleted.append(conversation_id)

    conversations = SimpleNamespace(get=get, delete=delete)
    client = cast(AsyncElevenLabs, SimpleNamespace(conversational_ai=SimpleNamespace(conversations=conversations)))
    assert isinstance(service.channel, WebChatChannel)
    with service.channel.subscribe(candidate) as events:
        await call.end("conv_1", client)
        # Their last words, which no turn answered, get a normal turn of the text chat: its reply lands there.
        while True:
            event = await asyncio.wait_for(events.get(), timeout=5)
            if isinstance(event, ChatMessage) and event.text == goodbye:
                break

    assert await chat(service, candidate) == [
        ("candidate", START, False),
        ("assistant", ASK_NAME, False),
        ("candidate", NAME, False),
        ("assistant", cut, True),
        ("candidate", stop, False),
        ("assistant", goodbye, False),
    ]
    assert deleted == ["conv_1"]


async def test_text_and_voice_turns_never_overlap(services: list[ScreeningService], database: str) -> None:
    model = Model({"hola": "Hola, Ana. ¿Empezamos?", START: ASK_NAME}, hold={"hola"})
    service, candidate, call = await call_on(services, database, model)

    found = await service.candidate(candidate)
    assert found is not None
    await service.receive(found[0], "hola", "typed-1")
    await model.called.wait()
    speaking = asyncio.create_task(call.answer(transcript(("user", START)), ElevenLabs().send_response, "conv_1"))
    await asyncio.sleep(0.2)
    assert len(model.sent) == 1  # the call waits for the text turn

    model.gate.set()
    await speaking
    assert (len(model.sent), model.most_running) == (2, 1)

"""Through the HTTP API, with the LLM stubbed: apply → chat (SSE) → outcome, the failure path, a recruiter deciding a
review case, and voice notes, with ElevenLabs stubbed too.

Needs the compose Postgres (`docker compose up -d --wait db`). The app runs under uvicorn, because an event stream
never ends and httpx's in-process transport only returns a response once it has.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from elevenlabs import SpeechToTextChunkResponseModel
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from screening.agent import LIVE_CALL, SPOKEN, screening_agent, summary_agent
from screening.config import DEFAULT_CLIENT_CONFIG, Language, Settings, load_client_config
from screening.db import Candidate, Message, Turn, make_sessionmaker
from screening.main import create_app
from screening.voice import MAX_NOTE_BYTES

pytestmark = pytest.mark.anyio

ACCESS_CODE = "demo-code"
RECRUITER_PASSWORD = "recruiter-pass"
EVERYTHING = {
    "full_name": "Lucía Martín García",
    "license": "yes",
    "postal_code": "28010",
    "availability": ["full_time"],
    "schedule": ["evening"],
    "experience_years": 2,
    "platforms": ["glovo"],
    "start_date": {"kind": "asap"},
}


def settings_for(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        screening_model="test",
        screening_fallback_models=[],
        chat_debounce_seconds=0.05,
        apply_access_code=ACCESS_CODE,
        recruiter_password=RECRUITER_PASSWORD,
        session_secret="test-session-secret",
        # httpx sends a Secure cookie over https only.
        session_https_only=False,
    )


def qualifying_model() -> FunctionModel:
    """Saves everything from the candidate's message, finishes, and closes."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned = [part.tool_name for part in messages[-1].parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("save_answers", EVERYTHING)])
        if returned == ["save_answers"]:
            return ModelResponse(parts=[ToolCallPart("finish_screening", {})])
        return ModelResponse(parts=[TextPart("Tu perfil cumple con los requisitos, Lucía.\n\nTe escribiremos pronto.")])

    return FunctionModel(respond)


def summary_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output = {"caveats": [], "notes": ["Puede empezar ya."]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


def reviewing_model(seen: list[list[ModelMessage]]) -> FunctionModel:
    """Asked for a person, it records the request and closes for review; after that it just answers."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        latest = messages[-1].parts
        returned = [part.tool_name for part in latest if isinstance(part, ToolReturnPart)]
        if any(isinstance(part, UserPromptPart) and "persona" in str(part.content) for part in latest):
            return ModelResponse(parts=[ToolCallPart("request_human", {"reason": "quiere hablar con alguien"})])
        if returned == ["request_human"]:
            return ModelResponse(parts=[ToolCallPart("finish_screening", {})])
        return ModelResponse(parts=[TextPart("Una persona del equipo revisará tu solicitud.")])

    return FunctionModel(respond)


def failing_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("the model is down")

    return FunctionModel(respond)


async def eventually(check: Callable[[], Awaitable[bool]]) -> None:
    """Polls: from outside the app there's no event to wait on for the server starting or the worker writing."""
    for _ in range(200):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out after 10 s")


@dataclass
class ElevenLabs:
    """ElevenLabs' batch speech-to-text and text-to-speech, as the voice-note routes call them: every note is heard as
    `heard`, and speech is the text's words."""

    heard: str
    transcribed: list[tuple[str, bytes, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    spoken: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.speech_to_text = SimpleNamespace(convert=self.convert, transcripts=SimpleNamespace(delete=self.delete))
        self.text_to_speech = SimpleNamespace(stream=self.stream)

    async def convert(self, *, file: tuple[str, bytes, str], **options: object) -> SpeechToTextChunkResponseModel:
        self.transcribed.append(file)
        return SpeechToTextChunkResponseModel(
            language_code="spa",
            language_probability=1,
            text=self.heard,
            words=[],
            transcription_id=f"transcript-{len(self.transcribed)}",
        )

    async def delete(self, transcription_id: str) -> None:
        self.deleted.append(transcription_id)

    async def stream(self, voice_id: str, *, text: str, **options: object) -> AsyncIterator[bytes]:
        self.spoken.append((voice_id, text))
        for word in text.split():
            yield f"{word} ".encode()


@asynccontextmanager
async def running_app(settings: Settings, elevenlabs: ElevenLabs | None = None) -> AsyncGenerator[httpx.AsyncClient]:
    app = create_app(settings)
    if elevenlabs is not None:
        # In place of the client the app builds from ELEVENLABS_API_KEY.
        app.state.elevenlabs = elevenlabs
    server = uvicorn.Server(uvicorn.Config(app, port=0, log_level="warning", timeout_graceful_shutdown=1))
    serving = asyncio.create_task(server.serve())

    async def started() -> bool:
        return server.started

    await eventually(started)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            yield client
    finally:
        server.should_exit = True
        await serving


async def sse(response: httpx.Response) -> AsyncIterator[tuple[str, str | None, dict]]:
    """(event, id, data) from a text/event-stream, skipping the keep-alive comments."""
    event, event_id, data = "message", None, []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event, event_id, json.loads("\n".join(data))
            event, event_id, data = "message", None, []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "id":
                event_id = value
            elif field == "data":
                data.append(value)


def application(**changes: object) -> dict:
    return {
        "submission_id": str(uuid.uuid4()),
        "full_name": "Lucía Martín García",
        "phone": "+34 600 000 000",
        "vacancy_location_id": "es-mad-chamberi",
        "language": "es",
        "access_code": ACCESS_CODE,
    } | changes


async def test_apply_chat_and_outcome_through_the_api(database_url: str) -> None:
    with screening_agent.override(model=qualifying_model()), summary_agent.override(model=summary_model()):
        async with running_app(settings_for(database_url)) as client:
            form = (await client.get("/api/apply-form")).json()
            assert form["access_code_required"] and {"es-mad-chamberi", "mx-cdmx-roma"} <= {
                vacancy["id"] for vacancy in form["vacancies"]
            }
            assert (await client.post("/api/applications", json=application(access_code="nope"))).status_code == 403

            body = application()
            created = (await client.post("/api/applications", json=body)).json()
            # A retried submit returns the same chat, not a second one.
            assert (await client.post("/api/applications", json=body)).json() == created
            token = created["chat_url"].removeprefix("/chat/")
            assert (await client.get(f"/api/chat/{token}")).json()["first_name"] == "Lucía"

            async with asyncio.timeout(10), client.stream("GET", f"/api/chat/{token}/events") as stream:
                events = sse(stream)
                event, first_id, first = await anext(events)
                assert first_id is not None
                assert (event, first["author"], first["quick_replies"]) == (
                    "message",
                    "assistant",
                    ["Empezar", "No me interesa"],
                )
                assert await anext(events) == ("status", None, {"status": "invited"})

                sent = await client.post(f"/api/chat/{token}/messages", json={"text": "sí, todo", "client_id": "m1"})
                assert sent.status_code == 202
                seen = []
                async for event, _, data in events:
                    seen.append((event, data))
                    if event == "status" and data["status"] == "completed":
                        break
            messages = [data for event, data in seen if event == "message"]
            assert [message["author"] for message in messages] == ["candidate", "assistant", "assistant"]
            assert messages[0]["client_id"] == "m1"
            assert ("typing", {"active": True}) in seen

            # A reconnect replays only what came after the last event it saw.
            async with (
                asyncio.timeout(10),
                client.stream("GET", f"/api/chat/{token}/events", headers={"Last-Event-ID": first_id}) as stream,
            ):
                _, replayed_id, replayed = await anext(sse(stream))
                assert replayed_id is not None and int(replayed_id) > int(first_id) and replayed["client_id"] == "m1"

            engine = create_async_engine(database_url)
            sessionmaker = make_sessionmaker(engine)

            async def summarized() -> bool:
                # The summary is written after the close is sent.
                async with sessionmaker() as session:
                    return bool(await session.scalar(select(Candidate.summary).where(Candidate.chat_token == token)))

            await eventually(summarized)
            async with sessionmaker() as session:
                candidate = (await session.scalars(select(Candidate).where(Candidate.chat_token == token))).one()
                turns = (await session.scalars(select(Turn).where(Turn.candidate_id == candidate.id))).all()
            await engine.dispose()
            assert (candidate.status, candidate.outcome) == ("completed", "qualified")
            assert candidate.summary == {"caveats": [], "notes": ["Puede empezar ya."]}
            # The first message is history too, so the agent sees what it answered.
            assert [turn.prompt_version is None for turn in turns] == [True, False]


async def test_a_failing_model_sends_the_notice_once_and_keeps_the_messages_pending(database_url: str) -> None:
    with screening_agent.override(model=failing_model()):
        async with running_app(settings_for(database_url)) as client:
            created = (await client.post("/api/applications", json=application())).json()
            token = created["chat_url"].removeprefix("/chat/")
            engine = create_async_engine(database_url)
            sessionmaker = make_sessionmaker(engine)

            for number, client_id in enumerate(["m1", "m2"], start=1):
                await client.post(f"/api/chat/{token}/messages", json={"text": "hola", "client_id": client_id})

                async def failed(times: int = number) -> bool:
                    async with sessionmaker() as session:
                        errors = select(Turn).where(
                            Turn.candidate_id == created["candidate_id"], Turn.error.is_not(None)
                        )
                        return len((await session.scalars(errors)).all()) >= times

                await eventually(failed)

            async with sessionmaker() as session:
                rows = (
                    await session.scalars(select(Message).where(Message.candidate_id == created["candidate_id"]))
                ).all()
            await engine.dispose()
            assert [row.text for row in rows if row.template_key == "failure"] == ["Dame un momento, por favor."]
            assert [row.client_id for row in rows if row.author == "candidate" and row.turn_id is None] == ["m1", "m2"]


async def test_a_recruiter_resolves_a_review_case(database_url: str) -> None:
    seen: list[list[ModelMessage]] = []
    with screening_agent.override(model=reviewing_model(seen)), summary_agent.override(model=summary_model()):
        async with running_app(settings_for(database_url)) as client:
            assert (await client.get("/api/recruiter/candidates")).status_code == 401
            wrong = {"name": "Ana", "password": "nope"}
            assert (await client.post("/api/recruiter/login", json=wrong)).status_code == 401
            login = {"name": "Ana", "password": RECRUITER_PASSWORD}
            assert (await client.post("/api/recruiter/login", json=login)).json() == {"name": "Ana"}
            assert (await client.get("/api/recruiter/me")).json() == {"name": "Ana"}

            created = (await client.post("/api/applications", json=application())).json()
            detail_url = f"/api/recruiter/candidates/{created['candidate_id']}"
            detail = (await client.get(detail_url)).json()
            assert detail["tab"] == "in_progress"
            assert detail["transcript"][0]["template_key"] == "first_message"
            listed = (
                await client.get("/api/recruiter/candidates", params={"tab": "in_progress", "q": "Martín"})
            ).json()
            assert created["candidate_id"] in [row["id"] for row in listed["candidates"]]

            decision = {"outcome": "qualified", "note": "Llamada hecha: todo bien."}
            # Nothing to decide on before the screening ends.
            assert (await client.post(f"{detail_url}/decision", json=decision)).status_code == 409

            token = created["chat_url"].removeprefix("/chat/")
            ask = {"text": "quiero hablar con una persona", "client_id": "m1"}
            await client.post(f"/api/chat/{token}/messages", json=ask)

            async def in_review() -> bool:
                return (await client.get(detail_url)).json()["tab"] == "review"

            await eventually(in_review)
            review = (await client.get("/api/recruiter/candidates", params={"tab": "review"})).json()
            row = next(row for row in review["candidates"] if row["id"] == created["candidate_id"])
            assert row["reasons"] == ["human_requested: quiere hablar con alguien"]
            assert review["counts"]["review"] >= 1

            decided = await client.post(f"{detail_url}/decision", json=decision)
            assert decided.json()["by"] == "Ana"
            # The decision is final.
            assert (await client.post(f"{detail_url}/decision", json=decision)).status_code == 409
            detail = (await client.get(detail_url)).json()
            assert detail["tab"] == "ready" and detail["decision"]["note"] == "Llamada hecha: todo bien."
            # The rule's outcome stays as it was; the decision sits next to it.
            assert detail["key_data"]["outcome"] == "needs_human_review"
            template = detail["transcript"][-1]
            assert template["template_key"] == "decision_qualified" and "tu perfil encaja" in template["text"]

            # Everything we send is in the model's history: the next turn sees the decision.
            await client.post(f"/api/chat/{token}/messages", json={"text": "¿y ahora qué?", "client_id": "m2"})

            def request_for(text: str) -> list[ModelMessage] | None:
                # Found by the candidate's text: at startup the app also resumes other tests' pending messages.
                for messages in seen:
                    if any(
                        isinstance(part, UserPromptPart) and text in str(part.content) for part in messages[-1].parts
                    ):
                        return messages
                return None

            async def next_turn_asked() -> bool:
                return request_for("¿y ahora qué?") is not None

            await eventually(next_turn_asked)
            request = request_for("¿y ahora qué?")
            assert request is not None
            history = [
                part.content
                for message in request
                if isinstance(message, ModelResponse)
                for part in message.parts
                if isinstance(part, TextPart)
            ]
            assert any("tu perfil encaja" in text for text in history)
            # And its context block gives the decision as the outcome, not the rules' review.
            prompt = request[-1].parts[-1]
            assert isinstance(prompt, UserPromptPart)
            assert "outcome: qualified (the hiring team's decision, final)" in str(prompt.content)
            # The numbers are tested in test_service.py; other tests' candidates share this database.
            assert (await client.get("/api/recruiter/metrics")).json()["applied"] >= 1

            assert (await client.post("/api/recruiter/logout")).status_code == 204
            assert (await client.get("/api/recruiter/me")).status_code == 401


async def stored(database_url: str, candidate_id: str) -> tuple[list[Message], list[Turn]]:
    engine = create_async_engine(database_url)
    async with make_sessionmaker(engine)() as session:
        messages = (await session.scalars(select(Message).where(Message.candidate_id == candidate_id))).all()
        turns = (await session.scalars(select(Turn).where(Turn.candidate_id == candidate_id))).all()
    await engine.dispose()
    return sorted(messages, key=lambda message: message.id), sorted(turns, key=lambda turn: turn.id)


async def test_a_voice_note_is_answered_like_a_typed_message_with_the_spoken_rules(database_url: str) -> None:
    read: list[tuple[str | None, str]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = messages[-1].parts[-1]
        assert isinstance(prompt, UserPromptPart)
        read.append((info.instructions, str(prompt.content)))
        return ModelResponse(parts=[TextPart("Entendido, Lucía.\n\n¿Tienes carnet de conducir en vigor?")])

    elevenlabs = ElevenLabs(heard="Sí, soy Lucía Martín García.")
    with screening_agent.override(model=FunctionModel(respond)):
        async with running_app(settings_for(database_url), elevenlabs) as client:
            created = (await client.post("/api/applications", json=application())).json()
            token = created["chat_url"].removeprefix("/chat/")
            sent = await client.post(
                f"/api/voice/chat/{token}/notes?client_id=n1",
                content=b"recorded audio",
                headers={"content-type": "audio/webm;codecs=opus"},
            )
            assert sent.status_code == 202

            async def answered() -> bool:
                messages, _ = await stored(database_url, created["candidate_id"])
                return messages[-1].author == "assistant" and messages[-1].kind == "text"

            await eventually(answered)
    messages, turns = await stored(database_url, created["candidate_id"])

    # Transcribed as recorded, and the transcript deleted at ElevenLabs. Its words are the candidate's message.
    assert elevenlabs.transcribed == [("note", b"recorded audio", "audio/webm;codecs=opus")]
    assert elevenlabs.deleted == ["transcript-1"]
    note = messages[1]
    assert (note.id, note.kind, note.text, note.client_id) == (sent.json()["id"], "voice_note", elevenlabs.heard, "n1")
    assert note.turn_id == turns[-1].id
    # The turn knows it's a voice note: the spoken rules, without a live call's, and one bubble, played as one audio.
    [(instructions, prompt)] = read
    assert instructions is not None and instructions.endswith(SPOKEN.strip()) and LIVE_CALL.strip() not in instructions
    assert "channel: voice_note" in prompt
    assert [message.text for message in messages[2:]] == ["Entendido, Lucía.\n\n¿Tienes carnet de conducir en vigor?"]


async def test_a_voice_note_with_no_words_or_too_big_runs_no_turn(database_url: str) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("no turn should run")

    elevenlabs = ElevenLabs(heard="  ")
    with screening_agent.override(model=FunctionModel(respond)):
        async with running_app(settings_for(database_url), elevenlabs) as client:
            created = (await client.post("/api/applications", json=application())).json()
            token = created["chat_url"].removeprefix("/chat/")
            silent = await client.post(f"/api/voice/chat/{token}/notes?client_id=n1", content=b"silence")
            too_big = await client.post(
                f"/api/voice/chat/{token}/notes?client_id=n2", content=bytes(MAX_NOTE_BYTES + 1)
            )
            # Longer than the debounce: a turn would have started by now.
            await asyncio.sleep(0.3)
    messages, turns = await stored(database_url, created["candidate_id"])

    assert (silent.status_code, too_big.status_code) == (422, 413)
    # The silent note was transcribed, and its transcript deleted; the big one was never sent.
    assert (len(elevenlabs.transcribed), elevenlabs.deleted) == (1, ["transcript-1"])
    # Only the first message, from the template.
    assert [(message.author, message.kind) for message in messages] == [("assistant", "template")]
    assert len(turns) == 1


async def test_our_messages_are_spoken_from_their_stored_text(database_url: str) -> None:
    elevenlabs = ElevenLabs(heard="hola")
    async with running_app(settings_for(database_url), elevenlabs) as client:
        created = (await client.post("/api/applications", json=application())).json()
        token = created["chat_url"].removeprefix("/chat/")
        await client.post(f"/api/chat/{token}/messages", json={"text": "hola", "client_id": "m1"})
        messages, _ = await stored(database_url, created["candidate_id"])
        first, candidates = messages[0], messages[1]

        spoken = await client.get(f"/api/voice/chat/{token}/messages/{first.id}/speech")
        # Only our messages, and only in their own chat.
        theirs = await client.get(f"/api/voice/chat/{token}/messages/{candidates.id}/speech")
        other = (await client.post("/api/applications", json=application())).json()["chat_url"].removeprefix("/chat/")
        elsewhere = await client.get(f"/api/voice/chat/{other}/messages/{first.id}/speech")

    assert (spoken.status_code, spoken.headers["content-type"]) == (200, "audio/mpeg")
    assert spoken.content == " ".join(first.text.split()).encode() + b" "
    voice = load_client_config(DEFAULT_CLIENT_CONFIG).voices[Language.ES]
    assert elevenlabs.spoken == [(voice, first.text)]
    assert (theirs.status_code, elsewhere.status_code) == (404, 404)

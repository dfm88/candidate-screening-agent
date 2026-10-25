"""Through the HTTP API, with the LLM stubbed: apply → chat (SSE) → outcome, the failure path, and a recruiter
deciding a review case.

Needs the compose Postgres (`docker compose up -d --wait db`). The app runs under uvicorn, because an event stream
never ends and httpx's in-process transport only returns a response once it has.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from screening.agent import screening_agent, summary_agent
from screening.config import Settings
from screening.db import Candidate, Message, Turn, make_sessionmaker
from screening.main import create_app

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


@asynccontextmanager
async def running_app(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), port=0, log_level="warning", timeout_graceful_shutdown=1)
    )
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

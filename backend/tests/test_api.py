"""Through the HTTP API, with the LLM stubbed: apply → chat (SSE) → outcome, and the failure path.

Needs the compose Postgres (`docker compose up -d --wait db`). The app runs under uvicorn, because an event stream
never ends and httpx's in-process transport only returns a response once it has.
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from unittest import mock

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from screening.agent import screening_agent, summary_agent
from screening.config import Settings
from screening.db import Candidate, Message, Turn, make_sessionmaker
from screening.main import create_app

pytestmark = pytest.mark.anyio

TEST_DATABASE = "screening_test"
ACCESS_CODE = "demo-code"
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


@pytest.fixture(scope="session")
def database_url() -> str:
    """A fresh database, built by the real migrations, so they're tested too."""
    server = Settings().database_url.rsplit("/", 1)[0]

    async def recreate() -> None:
        engine = create_async_engine(f"{server}/postgres", isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
                await connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(recreate())
    except OSError:
        pytest.skip("needs Postgres: docker compose up -d --wait db")
    url = f"{server}/{TEST_DATABASE}"
    with mock.patch.dict(os.environ, {"DATABASE_URL": url}):
        command.upgrade(Config("alembic.ini"), "head")
    return url


def settings_for(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        screening_model="test",
        screening_fallback_models=[],
        chat_debounce_seconds=0.05,
        apply_access_code=ACCESS_CODE,
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

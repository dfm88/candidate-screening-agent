"""The recruiters' MCP server (mcp_server.py) through the SDK's own client, against the app in-process and under a
deployed app's Host: each tool returns what the recruiter API returns, without the transcript or the chat token, and
any other path under /mcp is a 404, the SPA's catch-all included.

Needs the compose Postgres (`docker compose up -d --wait db`). Each test starts from empty tables, because the tab
counts and the metrics are over every candidate.
"""

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

from screening.config import Language, Settings
from screening.db import Candidate, Source
from screening.domain import Answers, AreaMatch, LicenseAnswer, Outcome, ScreeningState, Status
from screening.main import create_app
from screening.service import ScreeningService

pytestmark = pytest.mark.anyio

MCP_TOKEN = "test-mcp-token"
RECRUITER_PASSWORD = "recruiter-pass"
# A deployed app's Host, not localhost: the SDK's default DNS-rebinding check would answer it with a 421.
BASE_URL = "https://screening.example.run.app"


@pytest.fixture
async def app(database_url: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE candidates CASCADE"))
    await engine.dispose()
    # A built frontend, so a wrong token meets the SPA's catch-all as it would deployed.
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html>")
    settings = Settings(
        database_url=database_url,
        screening_model="test",
        screening_fallback_models=[],
        static_dir=tmp_path,
        recruiter_password=RECRUITER_PASSWORD,
        session_secret="test-session-secret",
        session_https_only=False,
        mcp_token=MCP_TOKEN,
    )
    app = create_app(settings)
    # httpx's in-process transport doesn't run the lifespan, where the MCP session manager starts.
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def recruiter_api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
        login = {"name": "Ana", "password": RECRUITER_PASSWORD}
        assert (await client.post("/api/recruiter/login", json=login)).status_code == 200
        yield client


async def decided_candidate(service: ScreeningService) -> Candidate:
    """A finished screening with the AI summary, a matching restaurant and a recruiter's decision."""
    candidate = await service.apply(
        external_id=f"test:{uuid.uuid4()}",
        source=Source.APPLY_FORM,
        full_name="Lucía Martín García",
        phone="+34 600 000 000",
        vacancy_location_id="es-mad-chamberi",
        language=Language.ES,
        whatsapp_opt_in=False,
    )
    answers = Answers(
        full_name="Lucía Martín García",
        license=LicenseAnswer.YES,
        area=AreaMatch(location_id="es-mad-chamberi", postal_code="28010"),
    )
    state = ScreeningState(status=Status.COMPLETED, outcome=Outcome.QUALIFIED, language=Language.ES, answers=answers)
    finished = {
        "status": Status.COMPLETED,
        "outcome": Outcome.QUALIFIED,
        "state": state.model_dump(mode="json"),
        "summary": {"caveats": [], "notes": ["Puede empezar ya."]},
        "completed_at": candidate.created_at,
    }
    async with service.sessionmaker() as session, session.begin():
        await session.execute(update(Candidate).where(Candidate.id == candidate.id).values(finished))
    await service.decide(candidate.id, Outcome.QUALIFIED, "Llamada hecha.", "Ana")
    return candidate


def structured(result: CallToolResult) -> dict:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def mcp_client(app: FastAPI, mode: str) -> Client:
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL)
    return Client(streamable_http_client(f"{BASE_URL}/mcp/{MCP_TOKEN}", http_client=http), mode=mode)


# Both ways a client connects: the 2026-07-28 discovery ("auto") and the initialize handshake of earlier versions.
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_the_tools_return_what_the_recruiter_api_returns(
    app: FastAPI, recruiter_api: httpx.AsyncClient, mode: str
) -> None:
    candidate = await decided_candidate(app.state.service)
    async with mcp_client(app, mode) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {"list_candidates", "get_candidate", "get_metrics"}
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools)

        for arguments in [{}, {"tab": "ready"}, {"tab": "review"}, {"q": "martín"}]:
            listed = structured(await client.call_tool("list_candidates", arguments))
            assert listed == (await recruiter_api.get("/api/recruiter/candidates", params=arguments)).json()
            assert listed["counts"]["ready"] == 1

        result = await client.call_tool("get_candidate", {"candidate_id": str(candidate.id)})
        detail = (await recruiter_api.get(f"/api/recruiter/candidates/{candidate.id}")).json()
        assert detail["transcript"] and detail["chat_url"] == f"/chat/{candidate.chat_token}"
        profile = structured(result)
        assert profile == {key: value for key, value in detail.items() if key not in {"chat_url", "transcript"}}
        assert profile["matched_location"]["id"] == "es-mad-chamberi" and profile["decision"]["by"] == "Ana"
        # Nowhere in what the assistant gets: neither the structured result nor the text next to it.
        sent = json.dumps(result.model_dump(mode="json"))
        assert candidate.chat_token not in sent and "transcript" not in sent

        unknown = await client.call_tool("get_candidate", {"candidate_id": str(uuid.uuid4())})
        assert unknown.is_error and isinstance(unknown.content[0], TextContent)
        assert "No candidate with that id." in unknown.content[0].text

        metrics = structured(await client.call_tool("get_metrics", {}))
        assert metrics == (await recruiter_api.get("/api/recruiter/metrics")).json()
        assert metrics["applied"] == 1


async def test_a_wrong_token_is_a_404(app: FastAPI) -> None:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {"accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
        assert (await client.post(f"/mcp/{MCP_TOKEN}", json=initialize, headers=headers)).status_code == 200
        for path in ["/mcp/wrong-token", f"/mcp/{MCP_TOKEN}x", f"/mcp/{MCP_TOKEN[:-1]}", "/mcp/"]:
            assert (await client.post(path, json=initialize, headers=headers)).status_code == 404, path
            assert (await client.get(path)).status_code == 404, path

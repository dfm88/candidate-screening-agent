import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime

import logfire
from elevenlabs import AsyncElevenLabs
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.sessions import SessionMiddleware

from screening import api, db, mcp_server, voice
from screening.agent import build_model
from screening.channels import WebChatChannel
from screening.config import DEFAULT_CLIENT_CONFIG, SERVICE_NAME, Settings, load_client_config
from screening.service import TICK_SECONDS, ScreeningService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logfire.configure(
        service_name=SERVICE_NAME,
        environment=settings.environment,
        send_to_logfire="if-token-present",
        # Cloud Run's front end adds a traceparent to every request, mostly unsampled. Continuing it would drop those
        # traces (the sampler follows the parent), and the turns too, since the worker inherits the request's context.
        distributed_tracing=False,
    )
    logfire.instrument_pydantic_ai(include_content=settings.llm_content_logging)
    engine = db.make_engine(settings.database_url)
    logfire.instrument_sqlalchemy(engine=engine)
    web_chat = WebChatChannel()
    service = ScreeningService(
        sessionmaker=db.make_sessionmaker(engine),
        config=load_client_config(DEFAULT_CLIENT_CONFIG),
        settings=settings,
        model=build_model(settings),
        channel=web_chat,
    )
    recruiter_mcp = mcp_server.build(service) if settings.mcp_token else None

    async def tick_forever() -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await service.tick(datetime.now(UTC))
            except Exception:
                # An edge: a failed tick (the database, say) is recorded, and the next one does its work.
                logfire.exception("tick failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        await service.resume()
        ticking = asyncio.create_task(tick_forever(), name="tick")
        # Starlette never runs a mounted app's lifespan, and the MCP app's requests need its session manager running.
        async with recruiter_mcp.session_manager.run() if recruiter_mcp else nullcontext():
            yield
        ticking.cancel()
        await asyncio.gather(ticking, return_exceptions=True)
        await service.stop()
        await engine.dispose()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.service = service
    app.state.web_chat = web_chat
    app.state.elevenlabs = AsyncElevenLabs(api_key=settings.elevenlabs_api_key) if settings.elevenlabs_api_key else None
    # Checked here, not in Settings: the evals and the CLI never use the recruiter tool, and a Settings validation
    # error would print the input values, secrets included.
    if settings.recruiter_password and not settings.session_secret:
        raise RuntimeError("RECRUITER_PASSWORD needs SESSION_SECRET, which signs the recruiter's session cookie")
    if settings.session_secret:
        # The recruiter's login: a signed, HttpOnly cookie, SameSite=Lax (the default) and the default 14-day max_age,
        # since reviewers may come back over several days. It's signed, not encrypted: it holds only the name typed.
        app.add_middleware(
            SessionMiddleware, secret_key=settings.session_secret, https_only=settings.session_https_only
        )
    app.include_router(api.router)
    app.include_router(api.recruiter_router)
    app.include_router(voice.router)
    if recruiter_mcp:
        # Mounted at /mcp with the token as the endpoint's path, so every other path under /mcp is the MCP app's 404,
        # never the SPA's catch-all. Stateless with JSON responses: Cloud Run has no sticky sessions, and each call is
        # one request and one answer. The SDK's DNS-rebinding check (on by default for localhost) would reject Cloud
        # Run's Host with a 421; it protects servers that trust their network, and here the token in the path is the
        # auth, behind a front end that routes by Host (the SDK's docs: "the honest configuration").
        app.mount(
            "/mcp",
            recruiter_mcp.streamable_http_app(
                streamable_http_path=f"/{settings.mcp_token}",
                stateless_http=True,
                json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            ),
        )
    # /mcp: the token sits in the path, which the request span would keep as its URL and name.
    logfire.instrument_fastapi(app, excluded_urls="/api/health,/mcp/")

    if settings.static_dir:
        _serve_frontend(app, settings)
    return app


def _serve_frontend(app: FastAPI, settings: Settings) -> None:
    static_dir = settings.static_dir
    assert static_dir is not None
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")
    index = static_dir / "index.html"

    # Client-side routes (/apply, /chat/:token, /recruiter) all resolve to the SPA entry point.
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)

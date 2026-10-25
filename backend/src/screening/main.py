from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import logfire
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from screening import api, db
from screening.agent import build_model
from screening.channels import WebChatChannel
from screening.config import DEFAULT_CLIENT_CONFIG, SERVICE_NAME, Settings, load_client_config
from screening.service import ScreeningService


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        await service.resume()
        yield
        await service.stop()
        await engine.dispose()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.service = service
    app.state.web_chat = web_chat
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
    logfire.instrument_fastapi(app, excluded_urls="/api/health")

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

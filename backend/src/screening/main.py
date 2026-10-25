from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import logfire
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
    app.include_router(api.router)
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

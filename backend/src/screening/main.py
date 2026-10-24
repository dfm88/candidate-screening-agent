from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import logfire
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from screening import api, db
from screening.config import SERVICE_NAME, Settings


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = db.make_sessionmaker(engine)
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

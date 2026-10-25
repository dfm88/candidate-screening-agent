import asyncio
import os
from unittest import mock

import pytest
from alembic import command
from alembic.config import Config
from pydantic_ai import models
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from screening.config import Settings

# Tests never call a real model; conversational behaviour is covered by the evals.
models.ALLOW_MODEL_REQUESTS = False

# The fixture below drops and recreates it: a second checkout testing against the same Postgres sets its own.
TEST_DATABASE = os.environ.get("TEST_DATABASE", "screening_test")


@pytest.fixture
def anyio_backend() -> str:
    # anyio's pytest plugin (installed with FastAPI) runs the async tests; asyncio is what the app uses.
    return "asyncio"


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

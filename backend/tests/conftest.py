import pytest
from pydantic_ai import models

# Tests never call a real model; conversational behaviour is covered by the evals.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def anyio_backend() -> str:
    # anyio's pytest plugin (installed with FastAPI) runs the async tests; asyncio is what the app uses.
    return "asyncio"

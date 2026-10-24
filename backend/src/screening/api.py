import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from screening import db

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    try:
        await db.ping(request.app.state.engine)
    except Exception:
        return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})


# Temporary: proves SSE survives the Cloud Run proxy before the real chat stream exists (removed in step 4).
@router.get("/health/stream", response_class=EventSourceResponse)
async def health_stream() -> AsyncIterator[ServerSentEvent]:
    for n in range(30):
        yield ServerSentEvent(event="tick", id=str(n), data=datetime.now(UTC).isoformat())
        await asyncio.sleep(2)

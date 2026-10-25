"""Channels deliver what the core sends. The web chat is the first adapter; WhatsApp and SMS are the next ones
(see the README), which is why this is a Protocol."""

import asyncio
from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Protocol
from uuid import UUID

from screening.contract import ChatEvent


class Channel(Protocol):
    async def publish(self, candidate_id: UUID, events: Sequence[ChatEvent]) -> None:
        """Deliver messages, typing and status to the candidate. An adapter skips what its channel can't show."""
        ...


class WebChatChannel:
    """Pushes events to the candidate's open SSE streams. Every message is stored before it's published, so a stream
    that wasn't open, or reconnects, replays it from the database."""

    def __init__(self) -> None:
        self._streams: defaultdict[UUID, set[asyncio.Queue[ChatEvent]]] = defaultdict(set)

    async def publish(self, candidate_id: UUID, events: Sequence[ChatEvent]) -> None:
        for queue in self._streams.get(candidate_id, ()):
            for event in events:
                queue.put_nowait(event)

    @contextmanager
    def subscribe(self, candidate_id: UUID) -> Generator[asyncio.Queue[ChatEvent]]:
        queue: asyncio.Queue[ChatEvent] = asyncio.Queue()
        self._streams[candidate_id].add(queue)
        try:
            yield queue
        finally:
            self._streams[candidate_id].discard(queue)
            if not self._streams[candidate_id]:
                del self._streams[candidate_id]

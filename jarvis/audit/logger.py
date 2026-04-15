"""AuditLogger — buffered async sink that writes to AuditRepo.

One queue, one background flusher. On each tick the flusher drains up to
`batch_size` events from the queue (if any) and writes them. `stop()`
drains remaining events before returning so shutdown never loses events.

The logger owns session lifecycle: it opens a fresh session per flush via
the provided `session_factory` and closes it when the flush finishes.
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.core.types import AuditEvent
from jarvis.persistence.repositories import AuditRepo

_log = logging.getLogger(__name__)


class AuditLogger:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        flush_interval_sec: float = 0.1,
        batch_size: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._flush_interval = flush_interval_sec
        self._batch_size = batch_size
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("AuditLogger already started")
        self._task = asyncio.create_task(self._run(), name="audit-logger")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        await self._task
        self._task = None

    async def emit(self, event: AuditEvent) -> None:
        if self._task is None:
            raise RuntimeError("AuditLogger not started")
        await self._queue.put(event)

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
                if self._stopping.is_set() and self._queue.empty():
                    return
            except Exception:
                _log.exception("audit logger loop error")

    async def _tick(self) -> None:
        buffer: list[AuditEvent] = []
        # Wait up to flush_interval for at least one event (unless stopping).
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
            buffer.append(first)
        except TimeoutError:
            return

        # Opportunistically drain the queue up to batch_size.
        while not self._queue.empty() and len(buffer) < self._batch_size:
            buffer.append(self._queue.get_nowait())

        await self._flush(buffer)

    async def _flush(self, events: list[AuditEvent]) -> None:
        async with self._session_factory() as session:
            repo = AuditRepo(session)
            await repo.write_many(events)

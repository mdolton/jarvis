"""EventCoalescer — buffers inbound external events into single agent turns.

Sits between event producers (the webhook route today; IMAP/pub-sub watchers
later) and the TriggerDispatcher. A burst of events on the same key produces
one merged EventTrigger after a short window instead of N back-to-back turns.

This path must never touch the MCP manager: flush tasks only call
`dispatcher.dispatch_event`, so no anyio cancel scope is entered or exited off
the MCP lifecycle task (see docs/superpowers/specs/2026-07-09-event-driven-
invocation-design.md).
"""

import asyncio
import logging
from dataclasses import dataclass

from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.types import EventTrigger

_log = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "An external event arrived. Review it and take any appropriate action per "
    "your standing instructions; if nothing is needed, summarize it briefly."
)


@dataclass(slots=True, frozen=True)
class _PendingEvent:
    source: str
    external_id: str
    prompt: str | None
    content: str


class EventCoalescer:
    """Per-key coalescing window over inbound events.

    `submit` is synchronous and must be called on the event loop; it never
    blocks on the agent turn, which runs on a background flush task under the
    dispatcher's existing concurrency gate.
    """

    def __init__(self, *, dispatcher: TriggerDispatcher, window_sec: float = 30.0) -> None:
        self._dispatcher = dispatcher
        self._window_sec = window_sec
        self._buffers: dict[str, list[_PendingEvent]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(
        self,
        *,
        source: str,
        external_id: str,
        content: str,
        prompt: str | None = None,
        coalesce_key: str | None = None,
    ) -> str:
        """Buffer one inbound event. Returns "queued" or "duplicate"."""
        # Dedup at intake against the dispatcher's LRU so a redelivered
        # webhook cannot extend or re-open a window.
        if not self._dispatcher.remember_if_new(external_id):
            _log.debug("dedup: dropping redelivered event %r", external_id)
            return "duplicate"

        key = source if coalesce_key is None else f"{source}:{coalesce_key}"
        self._buffers.setdefault(key, []).append(
            _PendingEvent(source=source, external_id=external_id, prompt=prompt, content=content)
        )
        if key not in self._tasks:
            task = asyncio.create_task(self._flush_after(key), name=f"event-coalesce-{key}")
            self._tasks[key] = task
        return "queued"

    async def shutdown(self) -> None:
        """Cancel pending windows; undelivered events are dropped."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._buffers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _flush_after(self, key: str) -> None:
        await asyncio.sleep(self._window_sec)
        # Pop buffer and task entry with no await in between: an event
        # arriving after this point opens a fresh window.
        events = self._buffers.pop(key, [])
        self._tasks.pop(key, None)
        if not events:
            return
        try:
            await self._dispatcher.dispatch_event(_merge(events))
        except Exception:
            _log.exception("event turn failed (key=%r, %d event(s))", key, len(events))


def _merge(events: list[_PendingEvent]) -> EventTrigger:
    first = events[0]
    if len(events) == 1:
        content = first.content
    else:
        sections = [
            f"Event {i}/{len(events)} (id {e.external_id}):\n{e.content}"
            for i, e in enumerate(events, start=1)
        ]
        content = "\n\n---\n\n".join(sections)
    prompt = next((e.prompt for e in events if e.prompt), None) or _DEFAULT_PROMPT
    return EventTrigger(
        source=first.source,
        # Distinct from the per-event ids already in the dedup LRU, yet
        # deterministic so an identical replayed burst still dedups.
        external_id=f"turn:{first.external_id}+{len(events)}",
        prompt=prompt,
        content=content,
    )

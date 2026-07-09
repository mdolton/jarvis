"""EventCoalescer: burst merging, intake dedup, per-key windows, shutdown."""

import asyncio

from jarvis.core.coalescer import EventCoalescer
from jarvis.core.types import EventTrigger


class _StubDispatcher:
    """Real LRU semantics, recorded dispatches, no agent run."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.dispatched: list[EventTrigger] = []

    def remember_if_new(self, external_id: str) -> bool:
        if external_id in self.seen:
            return False
        self.seen.add(external_id)
        return True

    async def dispatch_event(self, trigger: EventTrigger):
        if not self.remember_if_new(trigger.external_id):
            return None
        self.dispatched.append(trigger)
        return object()


async def _drain(coalescer: EventCoalescer, window: float) -> None:
    await asyncio.sleep(window + 0.05)


async def test_burst_coalesces_into_one_merged_turn():
    dispatcher = _StubDispatcher()
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=0.05)

    for i in range(3):
        status = coalescer.submit(
            source="email",
            external_id=f"msg-{i}",
            content=f"body {i}",
            prompt="Triage my mail." if i == 0 else None,
        )
        assert status == "queued"

    await _drain(coalescer, 0.05)

    assert len(dispatcher.dispatched) == 1
    trigger = dispatcher.dispatched[0]
    assert trigger.source == "email"
    assert trigger.prompt == "Triage my mail."
    assert trigger.external_id == "turn:msg-0+3"
    for i in range(3):
        assert f"body {i}" in trigger.content
    assert "Event 1/3" in trigger.content


async def test_single_event_content_passes_through_unwrapped():
    dispatcher = _StubDispatcher()
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=0.02)

    coalescer.submit(source="calendar", external_id="ev-1", content="Meeting moved to 3pm")
    await _drain(coalescer, 0.02)

    (trigger,) = dispatcher.dispatched
    assert trigger.content == "Meeting moved to 3pm"
    assert trigger.prompt  # default standing instruction fills in


async def test_redelivered_external_id_is_duplicate_and_never_fires_twice():
    dispatcher = _StubDispatcher()
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=0.02)

    assert coalescer.submit(source="email", external_id="msg-1", content="x") == "queued"
    assert coalescer.submit(source="email", external_id="msg-1", content="x") == "duplicate"
    await _drain(coalescer, 0.02)

    assert len(dispatcher.dispatched) == 1
    # Redelivery after the window flushed is still a duplicate (LRU remembers).
    assert coalescer.submit(source="email", external_id="msg-1", content="x") == "duplicate"
    await _drain(coalescer, 0.02)
    assert len(dispatcher.dispatched) == 1


async def test_distinct_sources_and_keys_get_separate_windows():
    dispatcher = _StubDispatcher()
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=0.05)

    coalescer.submit(source="email", external_id="m-1", content="a")
    coalescer.submit(source="calendar", external_id="c-1", content="b")
    coalescer.submit(source="email", external_id="m-2", content="c", coalesce_key="thread-9")
    await _drain(coalescer, 0.05)

    assert len(dispatcher.dispatched) == 3
    assert {t.source for t in dispatcher.dispatched} == {"email", "calendar"}


async def test_event_after_flush_opens_a_fresh_window():
    dispatcher = _StubDispatcher()
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=0.02)

    coalescer.submit(source="email", external_id="m-1", content="first")
    await _drain(coalescer, 0.02)
    coalescer.submit(source="email", external_id="m-2", content="second")
    await _drain(coalescer, 0.02)

    assert len(dispatcher.dispatched) == 2
    assert dispatcher.dispatched[0].content == "first"
    assert dispatcher.dispatched[1].content == "second"


async def test_shutdown_cancels_pending_window():
    dispatcher = _StubDispatcher()
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=30.0)

    coalescer.submit(source="email", external_id="m-1", content="never delivered")
    await coalescer.shutdown()

    assert dispatcher.dispatched == []


async def test_dispatch_failure_is_contained():
    dispatcher = _StubDispatcher()
    real_dispatch = dispatcher.dispatch_event
    calls = 0

    async def _flaky(trigger):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("runner blew up")
        return await real_dispatch(trigger)

    dispatcher.dispatch_event = _flaky  # type: ignore[method-assign]
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=0.02)

    coalescer.submit(source="email", external_id="m-1", content="x")
    await _drain(coalescer, 0.02)  # must not raise; error is logged

    # A failed turn doesn't wedge the window: later events still fire.
    coalescer.submit(source="email", external_id="m-2", content="y")
    await _drain(coalescer, 0.02)
    assert len(dispatcher.dispatched) == 1
    assert dispatcher.dispatched[0].content == "y"

"""NotificationGate — priority tiers, persisted daily budget, digest coalescing.

Covers the goal's acceptance criteria: a burst of low-priority events becomes
one digest entry instead of one ping each; P1 always interrupts; the budget
resets at the UTC day boundary and survives a restart (it lives in SQLite).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import OutboundMessage
from jarvis.core.output_router import NotificationGate, OutputRouter, Priority
from jarvis.core.types import ChannelKind
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import NotificationRepo
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


class _RecordingAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self, dispatcher) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _event_result(text: str, *, source: str = "email") -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=uuid4(),
        trigger_id=uuid4(),
        channel_kind=ChannelKind.EVENT,
        channel_ref=source,
    )


def _scheduled_result(text: str) -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=uuid4(),
        trigger_id=uuid4(),
        channel_kind=ChannelKind.SCHEDULED,
        channel_ref="sched",
    )


async def test_low_priority_burst_yields_one_digest_entry_not_n_pings(factory):
    """~20 P4 events in a burst -> zero standalone pings; the next scheduled
    digest send carries them as a single coalesced entry."""
    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=5)
    router = OutputRouter(
        adapters=[adapter], notification_gate=gate, event_notify_ref="user-1"
    )

    for i in range(20):
        await router.route(_event_result(f"[P4] minor update #{i}"))

    assert adapter.sent == []  # no pings

    scheduled = ScheduledOutputRouter(discord_adapter=adapter, notification_gate=gate)
    await scheduled.route(
        result=_scheduled_result("Daily brief body."),
        output_mode="discord",
        discord_user_id="user-1",
    )

    assert len(adapter.sent) == 1
    text = adapter.sent[0].text
    assert "Daily brief body." in text
    assert "While you were away:" in text
    # One coalesced entry for the whole burst, not 20 lines.
    digest_lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(digest_lines) == 1
    assert "20 updates" in digest_lines[0]
    assert "minor update #19" in digest_lines[0]  # latest wins


async def test_p1_delivers_immediately_even_when_budget_exhausted(factory):
    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=1)
    router = OutputRouter(
        adapters=[adapter], notification_gate=gate, event_notify_ref="user-1"
    )

    await router.route(_event_result("first ping"))  # spends the whole budget
    await router.route(_event_result("over budget"))  # queued
    assert len(adapter.sent) == 1

    await router.route(_event_result("[P1] server on fire"))
    assert len(adapter.sent) == 2
    # Marker stripped; autonomous sends carry a provenance prefix.
    assert adapter.sent[1].text == "⚙️ [event:email] server on fire"


async def test_p2_p3_respect_daily_budget(factory):
    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=3)
    router = OutputRouter(
        adapters=[adapter], notification_gate=gate, event_notify_ref="user-1"
    )

    for i in range(5):
        await router.route(_event_result(f"[P2] update {i}"))

    assert len(adapter.sent) == 3  # budget caps standalone pings
    async with factory() as session:
        rows = await NotificationRepo(session).claim_queued(at=datetime.now(UTC))
    assert len(rows) == 2  # the overflow waits for the digest


async def test_budget_resets_across_utc_day_boundary(factory):
    """Yesterday's sends don't count against today's budget."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    async with factory() as session:
        repo = NotificationRepo(session)
        for i in range(5):
            await repo.record_sent(
                priority=Priority.P3, source="event:email", text=f"old {i}", at=yesterday
            )

    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=5)
    router = OutputRouter(
        adapters=[adapter], notification_gate=gate, event_notify_ref="user-1"
    )

    await router.route(_event_result("fresh day, fresh budget"))
    assert len(adapter.sent) == 1


async def test_budget_survives_restart(factory):
    """The counter is read from SQLite, so a new gate instance (process
    restart) still sees today's spend."""
    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=1)
    router = OutputRouter(
        adapters=[adapter], notification_gate=gate, event_notify_ref="user-1"
    )
    await router.route(_event_result("spend it"))
    assert len(adapter.sent) == 1

    fresh_gate = NotificationGate(session_factory=factory, daily_budget=1)
    fresh_router = OutputRouter(
        adapters=[adapter], notification_gate=fresh_gate, event_notify_ref="user-1"
    )
    await fresh_router.route(_event_result("should queue"))
    assert len(adapter.sent) == 1


async def test_silent_event_is_dropped_entirely(factory):
    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=5)
    router = OutputRouter(
        adapters=[adapter], notification_gate=gate, event_notify_ref="user-1"
    )

    await router.route(_event_result("[SILENT] nothing to see"))

    assert adapter.sent == []
    async with factory() as session:
        rows = await NotificationRepo(session).claim_queued(at=datetime.now(UTC))
    assert rows == []  # not even queued


async def test_event_without_gate_stays_dashboard_only(factory):
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_event_result("hello"))

    assert adapter.sent == []


async def test_digest_drains_queue_once(factory):
    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=5)
    router = OutputRouter(
        adapters=[adapter], notification_gate=gate, event_notify_ref="user-1"
    )
    await router.route(_event_result("[P4] queued thing"))

    scheduled = ScheduledOutputRouter(discord_adapter=adapter, notification_gate=gate)
    for _ in range(2):
        await scheduled.route(
            result=_scheduled_result("Brief."),
            output_mode="discord",
            discord_user_id="user-1",
        )

    assert len(adapter.sent) == 2
    assert "While you were away:" in adapter.sent[0].text
    assert "While you were away:" not in adapter.sent[1].text  # already digested


async def test_noteworthy_scheduled_send_is_gated_over_budget(factory):
    adapter = _RecordingAdapter()
    gate = NotificationGate(session_factory=factory, daily_budget=1)
    scheduled = ScheduledOutputRouter(discord_adapter=adapter, notification_gate=gate)

    for i in range(3):
        await scheduled.route(
            result=_scheduled_result(f"[NOTEWORTHY] thing {i}"),
            output_mode="discord_if_noteworthy",
            discord_user_id="user-1",
            source="schedule:watcher",
        )

    assert len(adapter.sent) == 1  # budget of 1
    assert adapter.sent[0].text == "thing 0"

    # The suppressed ones ride the next digest.
    await scheduled.route(
        result=_scheduled_result("Brief."),
        output_mode="discord",
        discord_user_id="user-1",
    )
    assert "schedule:watcher" in adapter.sent[-1].text
    assert "2 updates" in adapter.sent[-1].text

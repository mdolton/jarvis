"""End-to-end: an event-triggered run leaves an autonomy.trace audit row.

The unit tests cover the routing logic with a recording fake; this test runs
the real AuditLogger flush cycle so the trace demonstrably lands in the
`audit_events` table — the same table the /audit page and the /events/stream
SSE feed read from.
"""

import pytest

from jarvis.agents.runner import AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.channels.base import OutboundMessage
from jarvis.core.output_router import NotificationGate, OutputRouter
from jarvis.core.types import AuditEventType, ChannelKind
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo


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


def _event_result(text: str) -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=None,
        trigger_id=None,
        channel_kind=ChannelKind.EVENT,
        channel_ref="email",
    )


async def test_event_run_lands_autonomy_trace_in_audit_table(factory):
    audit = AuditLogger(session_factory=factory)
    await audit.start()
    adapter = _RecordingAdapter()
    router = OutputRouter(
        adapters=[adapter],
        notification_gate=NotificationGate(session_factory=factory, daily_budget=5),
        event_notify_ref="user-1",
        audit=audit,
    )

    await router.route(_event_result("[P4] filed a receipt"))  # queued, not sent
    await audit.stop()  # drains the queue -> row is flushed

    assert adapter.sent == []
    async with factory() as session:
        rows = await AuditRepo(session).recent(types=[AuditEventType.AUTONOMY_TRACE], limit=10)
    assert len(rows) == 1
    assert rows[0].payload["delivery"] == "digest"
    assert rows[0].payload["source"] == "event:email"
    assert rows[0].payload["action"] == "[P4] filed a receipt"

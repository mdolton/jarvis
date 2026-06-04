from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from jarvis.actions.service import ActionService
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.types import (
    AuditEventType,
    ChannelKind,
    MessageRole,
    TriggerKind,
)
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    ActionRepo,
    AuditRepo,
    ConversationRepo,
    MessageRepo,
    TriggerRepo,
)


class _FakeRunState:
    def __init__(self, interruptions=None):
        self.interruptions = list(interruptions or [])
        self.approved_item = None
        self.rejected_item = None
        self.rejection_message = None

    def get_interruptions(self):
        return self.interruptions

    def approve(self, item):
        self.approved_item = item

    def reject(self, item, *, rejection_message=None):
        self.rejected_item = item
        self.rejection_message = rejection_message


class _FakeResult:
    def __init__(self) -> None:
        self.final_output = "resume complete"
        self.interruptions = []


@pytest_asyncio.fixture(loop_scope="function")
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    yield factory, audit
    await audit.stop()
    await engine.dispose()


async def _action(factory, *, conversation: bool = False):
    async with factory() as s:
        conversation_id = None
        trigger_id = None
        if conversation:
            conv = await ConversationRepo(s).find_or_create_open(
                channel_kind=ChannelKind.DASHBOARD,
                channel_ref="dashboard",
                idle_timeout_sec=900,
            )
            conversation_id = conv.id
            await MessageRepo(s).append(
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content="send it",
            )
            trigger = await TriggerRepo(s).record(
                kind=TriggerKind.MANUAL.value,
                source_ref="dashboard",
            )
            trigger_id = trigger.id

        return await ActionRepo(s).create_pending(
            conversation_id=conversation_id,
            trigger_id=trigger_id,
            channel_kind=ChannelKind.DASHBOARD.value,
            channel_ref="dashboard",
            server_name="gmail",
            tool_name="send_email",
            tool_call_id="call-1",
            arguments_json={"to": "me@example.com"},
            run_state_json={"state": "serialized"},
            approval_item_json={"raw_item": {"name": "send_email"}},
            model="test-model",
        )


async def test_approve_uses_restored_interruption_and_routes(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    run_mock = AsyncMock(return_value=_FakeResult())
    monkeypatch.setattr("jarvis.actions.service.Runner.run", run_mock)
    router = SimpleNamespace(route=AsyncMock())

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=router,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    result = await service.approve(action.id)

    assert state.approved_item is canonical_item
    assert result.final_output == "resume complete"
    router.route.assert_awaited_once()
    run_mock.assert_awaited_once()

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "completed"
        assert got.decision == "approved"
        msgs = await MessageRepo(s).history(action.conversation_id)
        assert msgs[-1].role == MessageRole.ASSISTANT.value
        assert msgs[-1].content == "resume complete"
        events = await AuditRepo(s).recent(
            types=[AuditEventType.ACTION_APPROVED, AuditEventType.ACTION_COMPLETED],
            limit=10,
        )
        assert {event.type for event in events} == {
            AuditEventType.ACTION_APPROVED.value,
            AuditEventType.ACTION_COMPLETED.value,
        }


async def test_reject_sends_reason_to_restored_interruption(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    await service.reject(action.id, reason="Do not send this.")

    assert state.rejected_item is canonical_item
    assert state.rejection_message == "Do not send this."

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "completed"
        assert got.decision == "rejected"
        assert got.decision_reason == "Do not send this."
        events = await AuditRepo(s).recent(
            types=[AuditEventType.ACTION_REJECTED, AuditEventType.ACTION_COMPLETED],
            limit=10,
        )
        assert {event.type for event in events} == {
            AuditEventType.ACTION_REJECTED.value,
            AuditEventType.ACTION_COMPLETED.value,
        }


async def test_decision_fails_cleanly_when_restored_state_has_no_interruptions(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory)
    state = _FakeRunState([])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock())

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    with pytest.raises(RuntimeError, match="no approval interruptions"):
        await service.approve(action.id)

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "failed"
        assert "no approval interruptions" in got.error
        events = await AuditRepo(s).recent(types=[AuditEventType.ACTION_FAILED], limit=10)
        assert len(events) == 1

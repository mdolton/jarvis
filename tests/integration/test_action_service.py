import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from jarvis.actions.service import ActionService
from jarvis.audit.logger import AuditLogger
from jarvis.channels.base import OutboundMessage
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
    ScheduleRepo,
    TriggerRepo,
)
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter


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


class _FakeInterruptedResult:
    def __init__(self, interruption) -> None:
        self.final_output = None
        self.interruptions = [interruption]

    def to_state(self):
        return SimpleNamespace(to_json=lambda: {"state": "second-serialized"})


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


class _RecordingMemoryService:
    def __init__(self) -> None:
        self.summarize_calls = []

    async def summarize_run(self, **kwargs):
        self.summarize_calls.append(kwargs)


class _BlockingMemoryService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def summarize_run(self, **kwargs):
        self.started.set()
        await self.release.wait()


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


async def _scheduled_action(factory):
    async with factory() as s:
        schedule = await ScheduleRepo(s).create(
            name="Morning digest",
            description="",
            cron_expr="0 9 * * *",
            timezone="UTC",
            prompt="send digest",
            output_mode="discord",
            notify_on_error=True,
            enabled=True,
            discord_user_id="111",
        )
        action = await ActionRepo(s).create_pending(
            conversation_id=None,
            trigger_id=None,
            channel_kind=ChannelKind.SCHEDULED.value,
            channel_ref=str(schedule.id),
            server_name="gmail",
            tool_name="send_email",
            tool_call_id="call-1",
            arguments_json={"to": "me@example.com"},
            run_state_json={"state": "serialized"},
            approval_item_json={"raw_item": {"name": "send_email"}},
            model="test-model",
        )
        return action, schedule


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
        for event in events:
            assert event.payload["action_id"] == str(action.id)
            assert event.payload["server_name"] == "gmail"
            assert event.payload["tool_name"] == "send_email"


async def test_approve_summarizes_real_resumed_output(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))
    memory_service = _RecordingMemoryService()

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
        memory_service=memory_service,
    )

    await service.approve(action.id)
    await service.drain_memory_tasks()

    assert len(memory_service.summarize_calls) == 1
    assert memory_service.summarize_calls[0]["conversation_id"] == action.conversation_id
    assert memory_service.summarize_calls[0]["user_prompt"] == "send it"
    assert memory_service.summarize_calls[0]["assistant_output"] == "resume complete"


async def test_approve_summarizes_against_original_action_prompt_not_later_user_message(
    monkeypatch, infra
):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    async with factory() as s:
        await TriggerRepo(s).record(
            kind=TriggerKind.MANUAL.value,
            source_ref="dashboard",
        )
        await MessageRepo(s).append(
            conversation_id=action.conversation_id,
            role=MessageRole.USER,
            content="later unrelated prompt",
        )

    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))
    memory_service = _RecordingMemoryService()

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
        memory_service=memory_service,
    )

    await service.approve(action.id)
    await service.drain_memory_tasks()

    assert len(memory_service.summarize_calls) == 1
    assert memory_service.summarize_calls[0]["user_prompt"] == "send it"


async def test_route_failure_after_success_leaves_action_completed(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))
    router = SimpleNamespace(route=AsyncMock(side_effect=RuntimeError("discord send failed")))

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=router,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    result = await service.approve(action.id)

    assert result.final_output == "resume complete"
    router.route.assert_awaited_once()

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "completed"
        assert got.error is None
        failed_events = await AuditRepo(s).recent(types=[AuditEventType.ACTION_FAILED], limit=10)
        assert failed_events == []


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
        for event in events:
            assert event.payload["action_id"] == str(action.id)
            assert event.payload["server_name"] == "gmail"
            assert event.payload["tool_name"] == "send_email"


async def test_second_interruption_creates_new_pending_action(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    first_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    second_item = SimpleNamespace(
        raw_item=SimpleNamespace(
            name="delete_message",
            call_id="call-2",
            arguments='{"id":"m-1"}',
            server_label="gmail",
        )
    )
    state = _FakeRunState([first_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr(
        "jarvis.actions.service.Runner.run",
        AsyncMock(return_value=_FakeInterruptedResult(second_item)),
    )
    router = SimpleNamespace(route=AsyncMock())

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=router,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    result = await service.approve(action.id)

    assert result.final_output.startswith("Action approval required: gmail.delete_message")
    router.route.assert_awaited_once()
    routed = router.route.await_args.args[0]
    assert routed.final_output == result.final_output

    await audit.stop()
    async with factory() as s:
        current = await ActionRepo(s).get(action.id)
        assert current.status == "completed"

        actions = await ActionRepo(s).list_recent()
        pending = [row for row in actions if row.status == "pending"]
        assert len(pending) == 1
        next_action = pending[0]
        assert next_action.id != action.id
        assert next_action.conversation_id == action.conversation_id
        assert next_action.trigger_id == action.trigger_id
        assert next_action.server_name == "gmail"
        assert next_action.tool_name == "delete_message"
        assert next_action.tool_call_id == "call-2"
        assert next_action.arguments_json == {"id": "m-1"}
        assert next_action.run_state_json == {"state": "second-serialized"}
        assert next_action.model == "test-model"
        assert str(next_action.id) in result.final_output

        msgs = await MessageRepo(s).history(action.conversation_id)
        assert msgs[-1].content == result.final_output

        events = await AuditRepo(s).recent(
            types=[AuditEventType.ACTION_CREATED, AuditEventType.ACTION_COMPLETED],
            limit=10,
        )
        created = next(event for event in events if event.type == "action.created")
        completed = next(event for event in events if event.type == "action.completed")
        assert created.payload["action_id"] == str(next_action.id)
        assert created.payload["server_name"] == "gmail"
        assert created.payload["tool_name"] == "delete_message"
        assert completed.payload["action_id"] == str(action.id)
        assert completed.payload["server_name"] == "gmail"
        assert completed.payload["tool_name"] == "send_email"


async def test_second_interruption_does_not_summarize_placeholder_notice(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    first_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    second_item = SimpleNamespace(
        raw_item=SimpleNamespace(
            name="delete_message",
            call_id="call-2",
            arguments='{"id":"m-1"}',
            server_label="gmail",
        )
    )
    state = _FakeRunState([first_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr(
        "jarvis.actions.service.Runner.run",
        AsyncMock(return_value=_FakeInterruptedResult(second_item)),
    )
    memory_service = _RecordingMemoryService()

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
        memory_service=memory_service,
    )

    await service.approve(action.id)
    await service.drain_memory_tasks()

    assert memory_service.summarize_calls == []


async def test_action_service_drain_waits_for_inflight_summary(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))
    memory_service = _BlockingMemoryService()

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
        memory_service=memory_service,
    )

    await service.approve(action.id)
    await memory_service.started.wait()

    drain_task = asyncio.create_task(service.drain_memory_tasks())
    await asyncio.sleep(0)
    assert not drain_task.done()

    memory_service.release.set()
    await drain_task


async def test_scheduled_resume_output_routes_through_scheduled_router(monkeypatch, infra):
    factory, audit = infra
    action, _schedule = await _scheduled_action(factory)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))
    adapter = _RecordingAdapter()

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        scheduled_output_router=ScheduledOutputRouter(discord_adapter=adapter),
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    result = await service.approve(action.id)

    assert result.final_output == "resume complete"
    assert len(adapter.sent) == 1
    assert adapter.sent[0].channel_kind == ChannelKind.DISCORD
    assert adapter.sent[0].channel_ref == "111"
    assert adapter.sent[0].text == "resume complete"

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "completed"


async def test_resume_failure_marks_failed_and_routes_notice(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory, conversation=True)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr(
        "jarvis.actions.service.Runner.run",
        AsyncMock(side_effect=RuntimeError("server disconnected")),
    )
    router = SimpleNamespace(route=AsyncMock())

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=router,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    with pytest.raises(RuntimeError, match="server disconnected"):
        await service.approve(action.id)

    router.route.assert_awaited_once()
    routed = router.route.await_args.args[0]
    assert (
        routed.final_output
        == f"Action failed: gmail.send_email ({action.id}). Check the Action Inbox for details."
    )
    assert "server disconnected" not in routed.final_output

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "failed"
        assert "server disconnected" in got.error
        events = await AuditRepo(s).recent(types=[AuditEventType.ACTION_FAILED], limit=10)
        assert len(events) == 1
        assert events[0].payload["action_id"] == str(action.id)
        assert events[0].payload["server_name"] == "gmail"
        assert events[0].payload["tool_name"] == "send_email"
        assert events[0].payload["error"] == "server disconnected"


async def test_scheduled_resume_failure_routes_safe_notice_through_scheduled_router(
    monkeypatch, infra
):
    factory, audit = infra
    action, _schedule = await _scheduled_action(factory)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    monkeypatch.setattr(
        "jarvis.actions.service.Runner.run",
        AsyncMock(side_effect=RuntimeError("server disconnected")),
    )
    adapter = _RecordingAdapter()

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        scheduled_output_router=ScheduledOutputRouter(discord_adapter=adapter),
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    with pytest.raises(RuntimeError, match="server disconnected"):
        await service.approve(action.id)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].channel_ref == "111"
    assert (
        adapter.sent[0].text
        == f"Action failed: gmail.send_email ({action.id}). Check the Action Inbox for details."
    )
    assert "server disconnected" not in adapter.sent[0].text

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "failed"
        assert "server disconnected" in got.error


async def test_interruption_call_id_mismatch_fails_closed(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory)
    mismatched_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-other"})
    state = _FakeRunState([mismatched_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )
    run_mock = AsyncMock(return_value=_FakeResult())
    monkeypatch.setattr("jarvis.actions.service.Runner.run", run_mock)

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    with pytest.raises(RuntimeError, match="no approval interruption matching tool call"):
        await service.approve(action.id)
    run_mock.assert_not_awaited()

    await audit.stop()
    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "failed"
        assert "call-1" in got.error


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
        assert events[0].payload["action_id"] == str(action.id)
        assert events[0].payload["server_name"] == "gmail"
        assert events[0].payload["tool_name"] == "send_email"


async def test_resume_timeout_marks_failed(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory)
    canonical_item = SimpleNamespace(raw_item={"name": "send_email", "call_id": "call-1"})
    state = _FakeRunState([canonical_item])
    monkeypatch.setattr(
        "jarvis.actions.service.run_state_from_json",
        AsyncMock(return_value=state),
    )

    async def hung_run(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr("jarvis.actions.service.Runner.run", hung_run)

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=SimpleNamespace(route=AsyncMock()),
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
        run_timeout_sec=0.05,
    )

    with pytest.raises(TimeoutError):
        await service.approve(action.id)

    async with factory() as s:
        row = await ActionRepo(s).get(action.id)
    assert row.status == "failed"
    assert "TimeoutError" in (row.error or "")

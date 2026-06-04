from datetime import UTC, datetime
from uuid import uuid4

import pytest

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import ConversationRow, TriggerRow
from jarvis.persistence.repositories import ActionRepo


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_action_repo_create_get_and_list_pending(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id="call-1",
        arguments_json={"to": "me@example.com"},
        run_state_json={"state": "serialized"},
        approval_item_json={"tool": "send_email"},
        model="test-model",
    )

    got = await repo.get(action.id)
    assert got is not None
    assert got.status == "pending"
    assert got.tool_name == "send_email"

    pending = await repo.list_pending()
    assert [a.id for a in pending] == [action.id]


async def test_action_repo_status_transitions(session):
    repo = ActionRepo(session)
    conversation_id = uuid4()
    trigger_id = uuid4()
    session.add(
        ConversationRow(
            id=conversation_id,
            channel_kind="discord",
            channel_ref="123",
            started_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
            status="open",
        )
    )
    session.add(
        TriggerRow(
            id=trigger_id,
            kind="discord",
            source_ref="123",
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()

    action = await repo.create_pending(
        conversation_id=conversation_id,
        trigger_id=trigger_id,
        channel_kind="discord",
        channel_ref="123",
        server_name="calendar",
        tool_name="create_event",
        tool_call_id=None,
        arguments_json={},
        run_state_json={"state": "serialized"},
        approval_item_json={"tool": "create_event"},
        model="test-model",
    )

    await repo.mark_running(action.id, decision="approved", decision_reason=None)
    running = await repo.get(action.id)
    assert running.status == "running"
    assert running.decision == "approved"
    assert running.decided_at is not None

    await repo.mark_completed(action.id)
    completed = await repo.get(action.id)
    assert completed.status == "completed"
    assert completed.completed_at is not None


async def test_action_repo_rejects_non_pending_decision(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id=None,
        arguments_json={},
        run_state_json={},
        approval_item_json={},
        model="test-model",
    )
    await repo.mark_running(action.id, decision="approved", decision_reason=None)
    await repo.mark_completed(action.id)
    action_id = action.id

    with pytest.raises(ValueError, match="pending"):
        await repo.mark_running(action_id, decision="approved", decision_reason=None)


async def test_action_repo_rejects_stale_running_decision(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id=None,
        arguments_json={},
        run_state_json={},
        approval_item_json={},
        model="test-model",
    )
    await repo.mark_running(action.id, decision="approved", decision_reason=None)
    action_id = action.id

    with pytest.raises(ValueError, match="pending"):
        await repo.mark_running(action_id, decision="rejected", decision_reason="too late")

    got = await repo.get(action_id)
    assert got.status == "running"
    assert got.decision == "approved"
    assert got.decision_reason is None


async def test_action_repo_rejects_pending_completion(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id=None,
        arguments_json={},
        run_state_json={},
        approval_item_json={},
        model="test-model",
    )
    action_id = action.id

    with pytest.raises(ValueError, match="running"):
        await repo.mark_completed(action_id)

    got = await repo.get(action_id)
    assert got.status == "pending"
    assert got.completed_at is None


async def test_action_repo_rejects_pending_failure(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id=None,
        arguments_json={},
        run_state_json={},
        approval_item_json={},
        model="test-model",
    )
    action_id = action.id

    with pytest.raises(ValueError, match="running"):
        await repo.mark_failed(action_id, "boom")

    got = await repo.get(action_id)
    assert got.status == "pending"
    assert got.completed_at is None
    assert got.error is None

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.core.types import ChannelKind, TriggerKind
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ActionRepo, ConversationRepo, TriggerRepo
from jarvis.web.app import create_app
from jarvis.web.routes import actions as actions_routes


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        conversation = await ConversationRepo(s).find_or_create_open(
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="dashboard",
            idle_timeout_sec=900,
        )
        trigger = await TriggerRepo(s).record(kind=TriggerKind.MANUAL.value, source_ref="dashboard")
        repo = ActionRepo(s)
        pending = await repo.create_pending(
            conversation_id=conversation.id,
            trigger_id=trigger.id,
            channel_kind="dashboard",
            channel_ref="dashboard",
            server_name="gmail",
            tool_name="send_email",
            tool_call_id="call-1",
            arguments_json={"subject": "Status", "to": "me@example.com"},
            run_state_json={"state": "serialized"},
            approval_item_json={"raw_item": {"name": "send_email"}},
            model="test-model",
        )
        failed = await repo.create_pending(
            conversation_id=None,
            trigger_id=None,
            channel_kind="scheduled",
            channel_ref="daily-digest",
            server_name="calendar",
            tool_name="create_event",
            tool_call_id="call-2",
            arguments_json={"title": "Focus"},
            run_state_json={"state": "serialized"},
            approval_item_json={"raw_item": {"name": "create_event"}},
            model="test-model",
        )
        await repo.mark_running(failed.id, decision="rejected", decision_reason="No")
        await repo.mark_failed(failed.id, "resume failed")

    ctx = SimpleNamespace(
        session_factory=factory,
        action_service=SimpleNamespace(approve=AsyncMock(), reject=AsyncMock()),
    )
    app = create_app(app_context=ctx)
    yield TestClient(app), pending.id, failed.id, conversation.id, trigger.id, ctx
    await engine.dispose()


def test_actions_page_lists_pending_first_then_newest(client):
    c, pending_id, failed_id, _, _, _ = client
    resp = c.get("/actions")

    assert resp.status_code == 200
    assert "create_event" in resp.text
    assert "send_email" in resp.text
    assert "me@example.com" in resp.text
    assert "Status" in resp.text
    assert str(failed_id) in resp.text
    assert str(pending_id) in resp.text
    assert resp.text.index("send_email") < resp.text.index("create_event")


def test_action_detail_renders_pending_action_and_controls(client):
    c, action_id, _, conversation_id, trigger_id, _ = client
    resp = c.get(f"/actions/{action_id}")

    assert resp.status_code == 200
    assert "gmail.send_email" in resp.text
    assert "dashboard / dashboard" in resp.text
    assert "me@example.com" in resp.text
    assert "test-model" in resp.text
    assert f'href="/conversations/{conversation_id}"' in resp.text
    assert str(conversation_id) in resp.text
    assert str(trigger_id) in resp.text
    assert f'action="/actions/{action_id}/approve"' in resp.text
    assert f'action="/actions/{action_id}/reject"' in resp.text


def test_approve_posts_to_service(client):
    c, action_id, _, _, _, ctx = client
    resp = c.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == f"/actions/{action_id}"
    ctx.action_service.approve.assert_awaited_once_with(action_id)


def test_approve_post_shields_service_resume(client, monkeypatch):
    c, action_id, _, _, _, ctx = client
    shielded = []
    real_shield = actions_routes.asyncio.shield

    def fake_shield(awaitable):
        shielded.append(awaitable)
        return real_shield(awaitable)

    monkeypatch.setattr(actions_routes.asyncio, "shield", fake_shield)

    resp = c.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert resp.status_code in (302, 303)
    assert len(shielded) == 1
    ctx.action_service.approve.assert_awaited_once_with(action_id)


def test_cross_origin_approve_post_is_blocked_before_service(client):
    c, action_id, _, _, _, ctx = client
    resp = c.post(
        f"/actions/{action_id}/approve",
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    ctx.action_service.approve.assert_not_awaited()


def test_reject_posts_to_service(client):
    c, action_id, _, _, _, ctx = client
    resp = c.post(
        f"/actions/{action_id}/reject",
        data={"reason": "No"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == f"/actions/{action_id}"
    ctx.action_service.reject.assert_awaited_once_with(action_id, reason="No")


def test_direct_approve_post_for_non_pending_action_returns_conflict(client):
    c, _, action_id, _, _, ctx = client
    ctx.action_service.approve.side_effect = ValueError("not pending")
    safe_client = TestClient(c.app, raise_server_exceptions=False)

    resp = safe_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert resp.status_code == 409
    assert resp.json() == {"detail": "action is not pending"}
    assert "location" not in resp.headers
    ctx.action_service.approve.assert_awaited_once_with(action_id)


def test_direct_reject_post_for_non_pending_action_returns_conflict(client):
    c, _, action_id, _, _, ctx = client
    ctx.action_service.reject.side_effect = ValueError("not pending")
    safe_client = TestClient(c.app, raise_server_exceptions=False)

    resp = safe_client.post(
        f"/actions/{action_id}/reject",
        data={"reason": "No"},
        follow_redirects=False,
    )

    assert resp.status_code == 409
    assert resp.json() == {"detail": "action is not pending"}
    assert "location" not in resp.headers
    ctx.action_service.reject.assert_awaited_once_with(action_id, reason="No")


def test_non_pending_action_detail_hides_controls_and_shows_error(client):
    c, _, action_id, _, _, _ = client
    resp = c.get(f"/actions/{action_id}")

    assert resp.status_code == 200
    assert "calendar.create_event" in resp.text
    assert "n/a" in resp.text
    assert "rejected" in resp.text
    assert "No" in resp.text
    assert "resume failed" in resp.text
    assert f'action="/actions/{action_id}/approve"' not in resp.text
    assert f'action="/actions/{action_id}/reject"' not in resp.text


def test_action_detail_returns_404_for_missing_action(client):
    c, _, _, _, _, _ = client
    resp = c.get(f"/actions/{uuid4()}")

    assert resp.status_code == 404

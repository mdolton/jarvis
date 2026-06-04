from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ActionRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        repo = ActionRepo(s)
        pending = await repo.create_pending(
            conversation_id=None,
            trigger_id=None,
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
    yield TestClient(app), pending.id, failed.id, ctx
    await engine.dispose()


def test_actions_page_lists_recent_actions_newest_first(client):
    c, pending_id, failed_id, _ = client
    resp = c.get("/actions")

    assert resp.status_code == 200
    assert "create_event" in resp.text
    assert "send_email" in resp.text
    assert str(failed_id) in resp.text
    assert str(pending_id) in resp.text
    assert resp.text.index("create_event") < resp.text.index("send_email")


def test_action_detail_renders_pending_action_and_controls(client):
    c, action_id, _, _ = client
    resp = c.get(f"/actions/{action_id}")

    assert resp.status_code == 200
    assert "gmail.send_email" in resp.text
    assert "dashboard / dashboard" in resp.text
    assert "me@example.com" in resp.text
    assert "test-model" in resp.text
    assert f'action="/actions/{action_id}/approve"' in resp.text
    assert f'action="/actions/{action_id}/reject"' in resp.text


def test_approve_posts_to_service(client):
    c, action_id, _, ctx = client
    resp = c.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == f"/actions/{action_id}"
    ctx.action_service.approve.assert_awaited_once_with(action_id)


def test_reject_posts_to_service(client):
    c, action_id, _, ctx = client
    resp = c.post(
        f"/actions/{action_id}/reject",
        data={"reason": "No"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == f"/actions/{action_id}"
    ctx.action_service.reject.assert_awaited_once_with(action_id, reason="No")


def test_non_pending_action_detail_hides_controls_and_shows_error(client):
    c, _, action_id, _ = client
    resp = c.get(f"/actions/{action_id}")

    assert resp.status_code == 200
    assert "calendar.create_event" in resp.text
    assert "rejected" in resp.text
    assert "No" in resp.text
    assert "resume failed" in resp.text
    assert f'action="/actions/{action_id}/approve"' not in resp.text
    assert f'action="/actions/{action_id}/reject"' not in resp.text

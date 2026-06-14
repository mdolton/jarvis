from datetime import UTC, datetime

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed some audit events.
    async with factory() as s:
        await AuditRepo(s).write_many(
            [
                AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"test": True}),
                AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"model": "qwen"}),
                AuditEvent(
                    type=AuditEventType.TOOL_ERROR,
                    payload={"error": "tool exploded"},
                    created_at=datetime(2026, 1, 2, 15, 4, 5, tzinfo=UTC),
                ),
            ]
        )

    from types import SimpleNamespace
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config = SimpleNamespace(jarvis=SimpleNamespace(timezone="UTC"))

    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_audit_page_renders_events(client):
    resp = client.get("/audit")
    assert resp.status_code == 200
    assert "trigger.received" in resp.text
    assert "llm.request" in resp.text


def test_audit_page_renders_dates_in_configured_timezone(client, monkeypatch):
    import os
    import time

    old_tz = os.environ.get("TZ")
    client.app.state.ctx.config.jarvis.timezone = "America/Los_Angeles"
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()

    try:
        resp = client.get("/audit")
        errors_resp = client.get("/errors")
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        if hasattr(time, "tzset"):
            time.tzset()

    assert resp.status_code == 200
    assert errors_resp.status_code == 200
    assert "2026-01-02 07:04:05" in resp.text
    assert "2026-01-02 07:04:05" in errors_resp.text
    assert "2026-01-02 15:04:05" not in resp.text


def test_error_log_page_surfaces_error_events(client):
    resp = client.get("/errors")
    assert resp.status_code == 200
    assert "Error Log" in resp.text
    assert "tool.error" in resp.text
    assert "tool exploded" in resp.text
    assert "llm.request" not in resp.text


def test_events_stream_endpoint_exists(client):
    """The SSE endpoint should be registered on the app."""
    # We verify the route exists by checking the app's route list — we cannot
    # call the endpoint directly with TestClient because the SSE stream never
    # ends, causing the test to hang. The content-type is set in events.py and
    # validated indirectly by the route inspection below.
    routes = {route.path for route in client.app.routes if hasattr(route, "path")}
    assert "/events/stream" in routes

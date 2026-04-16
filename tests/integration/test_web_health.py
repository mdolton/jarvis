from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def test_healthz_ok_without_context():
    """When no app_context is set (e.g., during early startup), healthz
    still returns 200 with a degraded status."""
    app = create_app(app_context=None)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_healthz_reports_db_and_mcp():
    ctx = MagicMock()
    ctx.session_factory = MagicMock()
    # Simulate a working DB check.
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    ctx.session_factory.return_value = mock_session

    ctx.mcp_manager.agent_mcp_servers.return_value = ["server1"]

    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["mcp_servers"] == 1

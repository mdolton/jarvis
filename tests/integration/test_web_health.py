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


def test_healthz_reports_component_diagnostics():
    from jarvis.agents.model_catalog import Catalog

    ctx = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    ctx.session_factory.return_value = mock_session
    ctx.mcp_manager.agent_mcp_servers.return_value = ["server1", "server2"]
    ctx.scheduler.active_job_count.return_value = 3
    ctx.model_catalog.list_models = AsyncMock(return_value=Catalog(models=["alpha"], ok=True))
    adapter = MagicMock(kind="discord")
    adapter.is_ready.return_value = True
    ctx.channel_adapters = [adapter]

    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/healthz")

    assert resp.status_code == 200
    data = resp.json()
    assert data["components"]["db"]["status"] == "ok"
    assert data["components"]["models"]["status"] == "ok"
    assert data["components"]["models"]["count"] == 1
    assert data["components"]["scheduler"]["active_jobs"] == 3
    assert data["components"]["discord"]["status"] == "ok"
    assert data["components"]["mcp"]["connected"] == 2

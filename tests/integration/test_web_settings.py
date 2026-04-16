from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def _mock_context():
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://x/v1"
    ctx.config.jarvis.llm.model = "m"
    ctx.config.jarvis.timezone = "UTC"
    ctx.config.jarvis.idle_timeout_sec = 900
    ctx.config.jarvis.max_concurrent_agents = 3
    ctx.config.jarvis.log_level = "INFO"
    ctx.config.channels.discord = None
    ctx.config.mcp_servers.servers = []
    return ctx


def test_settings_page_renders(tmp_path):
    ctx = _mock_context()
    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "900" in resp.text  # idle_timeout
    assert "UTC" in resp.text

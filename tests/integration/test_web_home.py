from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def _mock_context():
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://localhost:1234/v1"
    ctx.config.jarvis.llm.model = "qwen2.5"
    ctx.mcp_manager.agent_mcp_servers.return_value = ["s1"]
    ctx.scheduler.active_job_count.return_value = 2
    ctx.channel_adapters = [MagicMock(kind="discord")]
    return ctx


def test_home_page_renders_status(tmp_path):
    ctx = _mock_context()
    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "qwen2.5" in resp.text
    assert "localhost:1234" in resp.text

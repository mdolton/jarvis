from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from jarvis.web.app import create_app


@dataclass
class _RunResult:
    final_output: str


def _mock_context():
    from jarvis.agents.model_catalog import Catalog

    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://localhost:1234/v1"
    ctx.config.jarvis.llm.model = "qwen2.5"
    ctx.mcp_manager.agent_mcp_servers.return_value = ["s1"]
    ctx.scheduler.active_job_count.return_value = 2
    ctx.channel_adapters = [MagicMock(kind="discord")]
    ctx.dispatcher.dispatch_manual = AsyncMock(return_value=_RunResult("manual answer"))
    ctx.model_catalog.list_models = AsyncMock(return_value=Catalog(models=["qwen2.5"], ok=True))
    return ctx


def test_home_page_renders_status(tmp_path):
    ctx = _mock_context()
    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "qwen2.5" in resp.text
    assert "localhost:1234" in resp.text


def test_home_manual_run_executes_prompt(tmp_path):
    ctx = _mock_context()
    app = create_app(app_context=ctx)
    client = TestClient(app)

    resp = client.post("/manual-runs", data={"prompt": "summarize today"})

    assert resp.status_code == 200
    assert "manual answer" in resp.text
    ctx.dispatcher.dispatch_manual.assert_awaited_once_with(
        user="dashboard",
        prompt="summarize today",
    )


def test_home_page_renders_diagnostics(tmp_path):
    ctx = _mock_context()
    app = create_app(app_context=ctx)
    client = TestClient(app)

    resp = client.get("/")

    assert resp.status_code == 200
    assert "Diagnostics" in resp.text
    assert "Models" in resp.text
    assert "Scheduler" in resp.text

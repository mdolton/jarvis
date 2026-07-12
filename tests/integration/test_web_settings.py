from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from jarvis.agents.model_catalog import Catalog
from jarvis.web.app import create_app


def _mock_context(*, selection=None, ok=True, models=("alpha", "beta")):
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://x/v1"
    ctx.config.jarvis.llm.model = "cfg-model"
    ctx.config.jarvis.timezone = "UTC"
    ctx.config.jarvis.idle_timeout_sec = 900
    ctx.config.jarvis.max_concurrent_agents = 3
    ctx.config.jarvis.log_level = "INFO"
    ctx.config.channels.discord = None
    ctx.config.mcp_servers.servers = []

    ctx.model_store.selection.return_value = selection
    ctx.model_store.current.return_value = selection or "cfg-model"
    ctx.model_store.set = AsyncMock()
    ctx.model_catalog.list_models = AsyncMock(return_value=Catalog(models=list(models), ok=ok))
    ctx.audit.emit = AsyncMock()
    return ctx


def test_settings_page_lists_models(tmp_path):
    ctx = _mock_context()
    client = TestClient(create_app(app_context=ctx), headers={"origin": "http://testserver"})
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "alpha" in resp.text and "beta" in resp.text
    assert "UTC" in resp.text


def test_settings_page_flags_unavailable_selection():
    ctx = _mock_context(selection="ghost", ok=True, models=("alpha",))
    client = TestClient(create_app(app_context=ctx), headers={"origin": "http://testserver"})
    resp = client.get("/settings")
    assert "not available" in resp.text.lower()


def test_post_model_sets_specific():
    ctx = _mock_context()
    client = TestClient(create_app(app_context=ctx), headers={"origin": "http://testserver"})
    resp = client.post("/settings/model", data={"model": "alpha"}, follow_redirects=False)
    assert resp.status_code in (302, 303)
    ctx.model_store.set.assert_awaited_once_with("alpha")
    ctx.audit.emit.assert_awaited_once()


def test_post_model_empty_clears_to_default():
    ctx = _mock_context()
    client = TestClient(create_app(app_context=ctx), headers={"origin": "http://testserver"})
    resp = client.post("/settings/model", data={"model": ""}, follow_redirects=False)
    assert resp.status_code in (302, 303)
    ctx.model_store.set.assert_awaited_once_with(None)

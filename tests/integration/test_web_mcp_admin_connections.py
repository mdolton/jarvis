from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import generate_key
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock()
    ctx.config.secrets_key = generate_key().encode()
    ctx.audit = MagicMock()
    ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock()
    ctx.mcp_manager.connect_connection = AsyncMock()
    ctx.mcp_manager.disconnect = AsyncMock()
    app = create_app(app_context=ctx)
    c = TestClient(app)
    yield c
    await engine.dispose()


def test_add_connection_creates_row(client):
    resp = client.post("/mcp/connections/add",
                       data={"provider_key": "calendar", "label": "Work"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "Work" in client.get("/mcp").text


def test_disable_then_enable_connection(client):
    client.post("/mcp/connections/add", data={"provider_key": "gmail", "label": "Personal"},
                follow_redirects=False)
    page = client.get("/mcp").text
    import re
    cid = re.search(r'/mcp/connections/([0-9a-f-]{36})/disable', page).group(1)
    assert client.post(f"/mcp/connections/{cid}/disable", follow_redirects=False).status_code == 303
    client.app.state.ctx.mcp_manager.disconnect.assert_awaited()
    assert client.post(f"/mcp/connections/{cid}/enable", follow_redirects=False).status_code == 303

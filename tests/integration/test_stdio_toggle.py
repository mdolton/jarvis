from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, SettingsRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
        await MCPServerRepo(s).upsert(name="fs", transport="stdio")
    ctx = MagicMock(); ctx.session_factory = factory; ctx.catalog = ProviderCatalog(factory)
    ctx.audit = MagicMock(); ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock(); ctx.mcp_manager.disconnect = AsyncMock(); ctx.mcp_manager.connect_server = AsyncMock()
    # config.mcp_servers.servers used by the enable path
    ctx.config = MagicMock(); ctx.config.mcp_servers.servers = []
    app = create_app(app_context=ctx)
    c = TestClient(app); c._factory = factory
    yield c
    await engine.dispose()


def test_disable_stdio_persists_override(client):
    assert client.post("/mcp/stdio/fs/disable", follow_redirects=False).status_code == 303
    client.app.state.ctx.mcp_manager.disconnect.assert_awaited_with("fs")
    import asyncio
    async def read():
        async with client._factory() as s:
            return await SettingsRepo(s).get("mcp.stdio_disabled")
    assert "fs" in (asyncio.get_event_loop().run_until_complete(read()) or [])


def test_enable_stdio_clears_override(client):
    client.post("/mcp/stdio/fs/disable", follow_redirects=False)
    assert client.post("/mcp/stdio/fs/enable", follow_redirects=False).status_code == 303
    import asyncio
    async def read():
        async with client._factory() as s:
            return await SettingsRepo(s).get("mcp.stdio_disabled")
    assert "fs" not in (asyncio.get_event_loop().run_until_complete(read()) or [])

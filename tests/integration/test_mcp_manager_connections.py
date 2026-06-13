"""Manager attaches enabled OAuth connections at start, keyed by runtime_name."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest_asyncio

from mcp.types import Tool

from jarvis.config.schema import MCPServersConfig
from jarvis.mcp.manager import MCPManager
from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory


class _FakeSDK:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def list_tools(self): return [Tool(name="list_events", inputSchema={})]


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_enabled_connection_attaches_at_start(factory):
    key = generate_key().encode()
    async with factory() as s:
        await MCPConnectionRepo(s).create(
            provider_key="calendar", label="Work", runtime_name="calendar:work", scopes=["a"])
    async with factory() as s:
        conn = await MCPConnectionRepo(s).get_by_runtime_name("calendar:work")
        await MCPConnectionRepo(s).set_tokens(
            conn.id, access_token_enc=encrypt_blob(b"AT", key), refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1), scopes_granted=["a"])

    mgr = MCPManager(config=MCPServersConfig(servers=[]), session_factory=factory,
                     secrets_key=key, oauth_flow=None, catalog=ProviderCatalog(factory))
    with patch("jarvis.mcp.manager._build_streamable_http", return_value=_FakeSDK()):
        await mgr.start()
    try:
        assert "calendar:work" in mgr.agent_mcp_context()
    finally:
        await mgr.stop()

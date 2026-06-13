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
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def list_tools(self):
        return [Tool(name="list_events", inputSchema={})]


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


async def test_http_connection_attaches_without_bearer_injection(factory):
    key = generate_key().encode()
    # an http provider + a connection with static headers
    from jarvis.oauth.store import MCPProviderRepo

    async with factory() as s:
        await MCPProviderRepo(s).upsert(
            key="internal",
            display_name="Internal",
            kind="http",
            mcp_url="http://svc.local/mcp",
            builtin=False,
            auth_mode=None,
            oauth_metadata_url=None,
            pkce=True,
            send_resource_indicator=True,
            extra_auth_params={},
            default_scopes=[],
            header_names=["X-API-Key"],
        )
    import json

    async with factory() as s:
        await MCPConnectionRepo(s).create(
            provider_key="internal",
            label="Prod",
            runtime_name="internal:prod",
            headers_enc=encrypt_blob(json.dumps({"X-API-Key": "secret"}).encode(), key),
        )

    captured = {}

    def fake_build(url, headers, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        captured["kwargs"] = kwargs
        return _FakeSDK()

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        secrets_key=key,
        oauth_flow=object(),
        catalog=ProviderCatalog(factory),
    )
    with patch("jarvis.mcp.manager._build_streamable_http", side_effect=fake_build):
        await mgr.start()
    try:
        assert captured["headers"] == {"X-API-Key": "secret"}
        # http/sse: no bearer token holder and no oauth unauthorized_retry wired
        assert captured["kwargs"].get("token_holder") is None
        assert "unauthorized_retry" not in captured["kwargs"]
        assert "internal:prod" in mgr.agent_mcp_context()
    finally:
        await mgr.stop()


async def test_enabled_connection_attaches_at_start(factory):
    key = generate_key().encode()
    async with factory() as s:
        await MCPConnectionRepo(s).create(
            provider_key="calendar", label="Work", runtime_name="calendar:work", scopes=["a"]
        )
    async with factory() as s:
        conn = await MCPConnectionRepo(s).get_by_runtime_name("calendar:work")
        await MCPConnectionRepo(s).set_tokens(
            conn.id,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes_granted=["a"],
        )

    mgr = MCPManager(
        config=MCPServersConfig(servers=[]),
        session_factory=factory,
        secrets_key=key,
        oauth_flow=None,
        catalog=ProviderCatalog(factory),
    )
    with patch("jarvis.mcp.manager._build_streamable_http", return_value=_FakeSDK()):
        await mgr.start()
    try:
        assert "calendar:work" in mgr.agent_mcp_context()
    finally:
        await mgr.stop()

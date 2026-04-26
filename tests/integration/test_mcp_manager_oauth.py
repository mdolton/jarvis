"""MCPManager OAuth integration: replace, remove, isolation from YAML servers."""

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.config.schema import MCPServersConfig
from jarvis.mcp.manager import MCPManager
from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.store import OAuthCredentialsRepo
from jarvis.persistence.db import Base, create_engine, session_factory


class FakeSDKServer:
    """Duck-typed agents.mcp server used as a fake in tests."""

    def __init__(self, *, list_tools_returns=None, list_tools_raises=None):
        self._list_returns = list_tools_returns or []
        self._list_raises = list_tools_raises
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def list_tools(self):
        if self._list_raises:
            raise self._list_raises
        return self._list_returns


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_replace_oauth_server_swaps_sdk_object(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        first = FakeSDKServer()
        second = FakeSDKServer()
        builds = iter([first, second])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name: next(builds),
        )

        await mgr.replace_oauth_server(
            "fastmail", url="https://api.fastmail.com/mcp",
            headers={"Authorization": "Bearer A1"},
        )
        assert mgr.agent_mcp_servers() == [first]
        assert first.entered

        await mgr.replace_oauth_server(
            "fastmail", url="https://api.fastmail.com/mcp",
            headers={"Authorization": "Bearer A2"},
        )
        assert mgr.agent_mcp_servers() == [second]
        assert second.entered
        # Old one is closed (eventually). Allow event loop to settle.
        import asyncio
        await asyncio.sleep(0)
        assert first.exited
    finally:
        await mgr.stop()


async def test_replace_oauth_server_aborts_on_list_tools_failure(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        first = FakeSDKServer(list_tools_returns=[])
        broken = FakeSDKServer(list_tools_raises=RuntimeError("bad token"))
        builds = iter([first, broken])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name: next(builds),
        )
        await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A1"})
        with pytest.raises(RuntimeError, match="bad token"):
            await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A2"})
        # Old server still active.
        assert mgr.agent_mcp_servers() == [first]
        assert not first.exited
    finally:
        await mgr.stop()


async def test_remove_oauth_server_closes_and_drops(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        sdk = FakeSDKServer()
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name: sdk,
        )
        await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A"})
        await mgr.remove_oauth_server("fastmail")
        assert mgr.agent_mcp_servers() == []
        assert sdk.exited
    finally:
        await mgr.stop()


async def test_start_iterates_catalog_and_attaches_oauth_server(factory, monkeypatch):
    """When oauth_credentials has a valid Fastmail row, start() builds the SDK server."""
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="fastmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=None,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=[],
        )

    sdk = FakeSDKServer()
    monkeypatch.setattr("jarvis.mcp.manager._build_streamable_http", lambda url, headers, *, name: sdk)

    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory, secrets_key=key)
    await mgr.start()
    try:
        assert sdk.entered
        assert mgr.agent_mcp_servers() == [sdk]
    finally:
        await mgr.stop()


async def test_start_skips_oauth_provider_without_credentials(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory, secrets_key=generate_key().encode())
    builds = []
    monkeypatch.setattr(
        "jarvis.mcp.manager._build_streamable_http",
        lambda url, headers, *, name: builds.append(1),
    )
    await mgr.start()
    try:
        assert builds == []
        assert mgr.agent_mcp_servers() == []
    finally:
        await mgr.stop()

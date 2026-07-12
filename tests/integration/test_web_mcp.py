import re
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo
from jarvis.web.app import create_app


@dataclass(slots=True)
class _SlottedCtx:
    session_factory: object
    mcp_manager: object
    catalog: object


async def _seed_gmail_connection_with_tool(factory):
    """Seed a gmail provider + connection + connection-backed runtime server + tool.

    The tool table only renders under a *connection* in the Phase-1 template, so
    the policy test needs a connection (not a stdio server) to back the tool.
    """
    async with factory() as s:
        await seed_built_in_providers(s)
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="gmail", label="Default", runtime_name="gmail:default"
        )
    async with factory() as s:
        server = await MCPServerRepo(s).upsert(
            name="gmail:default",
            transport="http",
            source="connection",
            connection_id=conn.id,
        )
        await MCPServerRepo(s).set_status(server.id, status="connected", last_error=None)
        await MCPToolRepo(s).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="list_events", input_schema={}, read_only_hint=True)],
        )
    return conn


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed a stdio MCP server (renders in the stdio section).
    async with factory() as s:
        server = await MCPServerRepo(s).upsert(name="gcal", transport="stdio")
        await MCPServerRepo(s).set_status(server.id, status="connected", last_error=None)

    # Seed a gmail connection-backed server + tool (renders the policy form).
    await _seed_gmail_connection_with_tool(factory)

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)

    app = create_app(app_context=ctx)
    yield TestClient(app, headers={"origin": "http://testserver"})
    await engine.dispose()


def test_mcp_page_renders_server_and_tools(client):
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "gcal" in resp.text
    assert "list_events" in resp.text
    assert "connected" in resp.text.lower()


def test_mcp_tool_policy_can_be_set_and_cleared(client):
    resp = client.get("/mcp")
    assert resp.status_code == 200

    match = re.search(
        r'<form(?=[^>]+data-policy-tool="list_events")(?=[^>]+action="([^"]+)")', resp.text
    )
    assert match is not None
    action = match.group(1)

    set_resp = client.post(action, data={"policy_override": "confirm"}, follow_redirects=False)
    assert set_resp.status_code in (302, 303)
    assert "confirm" in client.get("/mcp").text

    clear_resp = client.post(action, data={"policy_override": ""}, follow_redirects=False)
    assert clear_resp.status_code in (302, 303)
    assert "auto-detect" in client.get("/mcp").text


def test_mcp_tool_policy_update_clears_runtime_policy_cache(client):
    client.app.state.ctx.mcp_manager = MagicMock()
    resp = client.get("/mcp")
    assert resp.status_code == 200

    match = re.search(
        r'<form(?=[^>]+data-policy-tool="list_events")(?=[^>]+action="([^"]+)")', resp.text
    )
    assert match is not None
    action = match.group(1)

    set_resp = client.post(action, data={"policy_override": "deny"}, follow_redirects=False)

    assert set_resp.status_code in (302, 303)
    client.app.state.ctx.mcp_manager.clear_policy_cache.assert_called_once_with()


@pytest_asyncio.fixture(loop_scope="function")
async def client_with_slotted_ctx(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    async with factory() as s:
        server = await MCPServerRepo(s).upsert(name="gcal", transport="stdio")
        await MCPServerRepo(s).set_status(server.id, status="connected", last_error=None)
    await _seed_gmail_connection_with_tool(factory)

    mcp_manager = MagicMock()
    app = create_app(
        app_context=_SlottedCtx(
            session_factory=factory,
            mcp_manager=mcp_manager,
            catalog=ProviderCatalog(factory),
        )
    )
    yield TestClient(app, headers={"origin": "http://testserver"}), mcp_manager
    await engine.dispose()


def test_mcp_tool_policy_update_clears_runtime_policy_cache_for_slotted_context(
    client_with_slotted_ctx,
):
    client, mcp_manager = client_with_slotted_ctx
    resp = client.get("/mcp")
    assert resp.status_code == 200

    match = re.search(
        r'<form(?=[^>]+data-policy-tool="list_events")(?=[^>]+action="([^"]+)")', resp.text
    )
    assert match is not None
    action = match.group(1)

    set_resp = client.post(action, data={"policy_override": "deny"}, follow_redirects=False)

    assert set_resp.status_code in (302, 303)
    mcp_manager.clear_policy_cache.assert_called_once_with()


@pytest_asyncio.fixture(loop_scope="function")
async def client_with_providers(tmp_path):
    """Seeds built-in providers but no connections."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    app = create_app(app_context=ctx)
    yield TestClient(app, headers={"origin": "http://testserver"}), factory
    await engine.dispose()


def test_mcp_page_lists_providers(client_with_providers):
    client, _ = client_with_providers
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "Gmail" in resp.text
    assert "Google Calendar" in resp.text


@pytest_asyncio.fixture(loop_scope="function")
async def client_with_connection(tmp_path):
    """A gmail connection that is authorized (has tokens) -> Disconnect shown."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="gmail", label="Default", runtime_name="gmail:default"
        )
    cid = conn.id
    async with factory() as s:
        from datetime import UTC, datetime, timedelta

        await MCPConnectionRepo(s).set_tokens(
            cid,
            access_token_enc=b"at",
            refresh_token_enc=b"rt",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes_granted=[],
        )
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    app = create_app(app_context=ctx)
    yield TestClient(app, headers={"origin": "http://testserver"}), cid
    await engine.dispose()


def test_mcp_page_shows_disconnect_when_connection_authorized(client_with_connection):
    client, cid = client_with_connection
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "Disconnect" in resp.text
    assert f"/oauth/disconnect/{cid}" in resp.text


@pytest_asyncio.fixture(loop_scope="function")
async def client_with_needs_reauth(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="gmail", label="Default", runtime_name="gmail:default"
        )
    cid = conn.id
    async with factory() as s:
        from datetime import UTC, datetime, timedelta

        await MCPConnectionRepo(s).set_tokens(
            cid,
            access_token_enc=b"at",
            refresh_token_enc=b"rt",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes_granted=[],
        )
    async with factory() as s:
        await MCPConnectionRepo(s).set_status(
            cid, status="needs_reauth", last_error="invalid_grant"
        )
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    app = create_app(app_context=ctx)
    yield TestClient(app, headers={"origin": "http://testserver"}), cid
    await engine.dispose()


def test_mcp_page_shows_reconnect_on_needs_reauth(client_with_needs_reauth):
    client, cid = client_with_needs_reauth
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "invalid_grant" in resp.text
    assert f"/oauth/connect/{cid}" in resp.text  # Connect link shown for needs_reauth

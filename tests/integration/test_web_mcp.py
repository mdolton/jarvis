import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.oauth.store import OAuthCredentialsRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed an MCP server + tool.
    async with factory() as s:
        server = await MCPServerRepo(s).upsert(name="gcal", transport="stdio")
        await MCPServerRepo(s).set_status(server.id, status="connected", last_error=None)
        await MCPToolRepo(s).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="list_events", input_schema={}, read_only_hint=True)],
        )

    ctx = MagicMock()
    ctx.session_factory = factory

    app = create_app(app_context=ctx)
    yield TestClient(app)
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
async def client_no_oauth(tmp_path):
    """Like `client` but with no oauth_credentials rows seeded."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    ctx = MagicMock()
    ctx.session_factory = factory
    app = create_app(app_context=ctx)
    yield TestClient(app), factory
    await engine.dispose()


def test_mcp_page_lists_oauth_providers_disconnected_by_default(client_no_oauth):
    client, _ = client_no_oauth
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "Fastmail" in resp.text
    assert 'href="/oauth/connect/fastmail"' in resp.text


@pytest_asyncio.fixture(loop_scope="function")
async def client_with_connected_oauth(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    now = datetime.now(UTC)
    async with factory() as s:
        await OAuthCredentialsRepo(s).upsert(
            provider_key="fastmail",
            client_id_enc=b"cid",
            client_secret_enc=None,
            access_token_enc=b"at",
            refresh_token_enc=b"rt",
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=[],
        )
    ctx = MagicMock()
    ctx.session_factory = factory
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_mcp_page_shows_connected_pill_when_credentials_present(client_with_connected_oauth):
    resp = client_with_connected_oauth.get("/mcp")
    assert resp.status_code == 200
    assert "Connected" in resp.text
    assert "Disconnect" in resp.text


@pytest_asyncio.fixture(loop_scope="function")
async def client_with_needs_reauth(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    now = datetime.now(UTC)
    async with factory() as s:
        repo = OAuthCredentialsRepo(s)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid",
            client_secret_enc=None,
            access_token_enc=b"at",
            refresh_token_enc=b"rt",
            token_expires_at=now,
            scopes_granted=[],
        )
        await repo.set_status("fastmail", status="needs_reauth", last_error="invalid_grant")
    ctx = MagicMock()
    ctx.session_factory = factory
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_mcp_page_shows_needs_reauth_banner(client_with_needs_reauth):
    resp = client_with_needs_reauth.get("/mcp")
    assert resp.status_code == 200
    assert "Re-authorization" in resp.text
    assert "invalid_grant" in resp.text
    assert 'href="/oauth/connect/fastmail"' in resp.text  # Reconnect link

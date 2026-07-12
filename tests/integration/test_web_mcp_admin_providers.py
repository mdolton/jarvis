import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import decrypt_blob, generate_key
from jarvis.oauth.store import MCPConnectionRepo
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
    ctx.mcp_manager.disconnect = AsyncMock()
    app = create_app(app_context=ctx)
    c = TestClient(app, headers={"origin": "http://testserver"})
    c._factory = factory
    yield c
    await engine.dispose()


def test_add_oauth_provider(client):
    resp = client.post(
        "/mcp/providers/add",
        data={
            "key": "notion",
            "display_name": "Notion",
            "kind": "oauth",
            "mcp_url": "https://mcp.notion.com/mcp",
            "auth_mode": "dcr",
            "oauth_metadata_url": "https://mcp.notion.com/.well-known/oauth-authorization-server",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Notion" in client.get("/mcp").text


def test_add_provider_rejects_stdio_kind(client):
    resp = client.post(
        "/mcp/providers/add",
        data={"key": "x", "display_name": "X", "kind": "stdio", "mcp_url": "y"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_remove_provider_refused_when_connections_exist(client):
    async def seed():
        async with client._factory() as s:
            await MCPConnectionRepo(s).create(
                provider_key="gmail", label="P", runtime_name="gmail:p"
            )

    asyncio.get_event_loop().run_until_complete(seed())
    resp = client.post("/mcp/providers/gmail/remove", follow_redirects=False)
    assert resp.status_code == 400


def test_builtin_provider_cannot_be_removed(client):
    resp = client.post("/mcp/providers/calendar/remove", follow_redirects=False)
    assert resp.status_code == 400


def test_edit_credentials_updates_all_connections(client):
    secrets_key = client.app.state.ctx.config.secrets_key

    async def seed():
        async with client._factory() as s:
            repo = MCPConnectionRepo(s)
            await repo.create(provider_key="gmail", label="Work", runtime_name="gmail:work")
            await repo.create(provider_key="gmail", label="Personal", runtime_name="gmail:personal")

    asyncio.get_event_loop().run_until_complete(seed())

    resp = client.post(
        "/mcp/providers/gmail/edit-credentials",
        data={"client_id": "APP-CID", "client_secret": "APP-SEC"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async def check():
        async with client._factory() as s:
            conns = await MCPConnectionRepo(s).list_for_provider("gmail")
        assert len(conns) == 2
        for conn in conns:
            assert conn.client_id_enc is not None
            assert decrypt_blob(conn.client_id_enc, secrets_key) == b"APP-CID"

    asyncio.get_event_loop().run_until_complete(check())


def test_edit_credentials_400_when_no_connections(client):
    resp = client.post(
        "/mcp/providers/fastmail/edit-credentials",
        data={"client_id": "cid", "client_secret": "sec"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "no connections" in resp.text

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import decrypt_blob, generate_key
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
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
    c = TestClient(app, headers={"origin": "http://testserver"})
    yield c
    await engine.dispose()


def test_add_connection_creates_row(client):
    resp = client.post(
        "/mcp/connections/add",
        data={"provider_key": "calendar", "label": "Work"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Work" in client.get("/mcp").text


def test_add_http_connection_encrypts_headers_and_calls_manager(client):
    secrets_key = client.app.state.ctx.config.secrets_key

    async def seed_http_provider():
        async with client.app.state.ctx.session_factory() as s:
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
                header_names=["Authorization"],
            )

    asyncio.get_event_loop().run_until_complete(seed_http_provider())

    resp = client.post(
        "/mcp/connections/add",
        data={
            "provider_key": "internal",
            "label": "Prod",
            "headers": "Authorization: Bearer tok123\nX-Env: prod",
            "url_override": "http://prod.svc.local/mcp",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    client.app.state.ctx.mcp_manager.connect_connection.assert_awaited()

    async def check():
        async with client.app.state.ctx.session_factory() as s:
            conns = await MCPConnectionRepo(s).list_for_provider("internal")
        assert len(conns) == 1
        conn = conns[0]
        assert conn.url_override == "http://prod.svc.local/mcp"
        assert conn.headers_enc is not None
        headers = json.loads(decrypt_blob(conn.headers_enc, secrets_key))
        assert headers == {"Authorization": "Bearer tok123", "X-Env": "prod"}

    asyncio.get_event_loop().run_until_complete(check())


def test_disable_then_enable_connection(client):
    client.post(
        "/mcp/connections/add",
        data={"provider_key": "gmail", "label": "Personal"},
        follow_redirects=False,
    )
    page = client.get("/mcp").text
    import re

    cid = re.search(r"/mcp/connections/([0-9a-f-]{36})/disable", page).group(1)
    assert client.post(f"/mcp/connections/{cid}/disable", follow_redirects=False).status_code == 303
    client.app.state.ctx.mcp_manager.disconnect.assert_awaited()
    assert client.post(f"/mcp/connections/{cid}/enable", follow_redirects=False).status_code == 303

"""Add a DCR oauth provider, add a connection, kick off connect (start_authorization)."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app

META = {
    "authorization_endpoint": "https://ex.com/auth",
    "token_endpoint": "https://ex.com/token",
    "registration_endpoint": "https://ex.com/register",
    "code_challenge_methods_supported": ["S256"],
}


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)

    def handler(request):
        if request.url.path.endswith(".well-known/oauth-authorization-server"):
            return httpx.Response(200, json=META)
        if request.url.path == "/register":
            return httpx.Response(201, json={"client_id": "dcr-cid"})
        return httpx.Response(404)

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock()
    ctx.config.secrets_key = generate_key().encode()
    ctx.audit = MagicMock()
    ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock()
    ctx.mcp_manager.connect_connection = AsyncMock()
    ctx.oauth_flow = OAuthFlow(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_factory=factory,
        base_url="http://localhost:8080",
        secrets_key=ctx.config.secrets_key,
        catalog=ctx.catalog,
    )
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_add_provider_connection_then_connect_redirects_to_consent(client):
    client.post(
        "/mcp/providers/add",
        data={
            "key": "ex",
            "display_name": "Example",
            "kind": "oauth",
            "mcp_url": "https://ex.com/mcp",
            "auth_mode": "dcr",
            "oauth_metadata_url": "https://ex.com/.well-known/oauth-authorization-server",
        },
        follow_redirects=False,
    )
    client.post(
        "/mcp/connections/add", data={"provider_key": "ex", "label": "Mine"}, follow_redirects=False
    )
    page = client.get("/mcp").text
    import re

    m = re.search(r"/oauth/connect/([0-9a-f-]{36})", page)
    assert m is not None, "no connect link rendered for the new connection"
    cid = m.group(1)
    resp = client.get(f"/oauth/connect/{cid}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://ex.com/auth")

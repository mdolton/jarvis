"""POST /mcp/providers/discover returns an HTMX fragment that prefills the form."""

from unittest.mock import MagicMock

import httpx
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog
from jarvis.oauth.crypto import generate_key
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app

AS_META = {
    "authorization_endpoint": "https://as.example.com/auth",
    "token_endpoint": "https://as.example.com/token",
    "registration_endpoint": "https://as.example.com/register",
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["read"],
}


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META)
        return httpx.Response(404)

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock()
    ctx.config.secrets_key = generate_key().encode()
    ctx.oauth_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_discover_route_returns_fragment_with_metadata(client):
    resp = client.post("/mcp/providers/discover", data={"mcp_url": "https://mcp.example.com"})
    assert resp.status_code == 200
    body = resp.text
    assert "https://as.example.com/.well-known/oauth-authorization-server" in body
    assert "dcr" in body
    # OOB swap targets the form input by id.
    assert 'id="oauth_metadata_url"' in body
    assert 'hx-swap-oob="true"' in body

"""Web routes for OAuth connect/callback/disconnect."""

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.oauth.crypto import generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


class _Ctx:
    """Tiny stand-in for AppContext exposing only what oauth routes need."""

    def __init__(self, session_factory_, oauth_flow):
        self.session_factory = session_factory_
        self.oauth_flow = oauth_flow
        self.mcp_manager = None  # set in tests that need it


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


def make_app(ctx) -> TestClient:
    app = create_app(app_context=ctx)
    return TestClient(app)


def fastmail_metadata():
    return {
        "issuer": "https://api.fastmail.com",
        "authorization_endpoint": "https://api.fastmail.com/oauth/authorize",
        "token_endpoint": "https://api.fastmail.com/oauth/token",
        "registration_endpoint": "https://api.fastmail.com/oauth/register",
        "revocation_endpoint": "https://api.fastmail.com/oauth/revoke",
        "code_challenge_methods_supported": ["S256"],
    }


async def test_connect_returns_302_to_consent_url(factory):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid"})
        return httpx.Response(404)

    flow = OAuthFlow(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_factory=factory,
        base_url="http://localhost:8080",
        secrets_key=generate_key().encode(),
    )
    ctx = _Ctx(factory, flow)

    client = make_app(ctx)
    r = client.get("/oauth/connect/fastmail", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://api.fastmail.com/oauth/authorize")


async def test_connect_unknown_provider_returns_404(factory):
    flow = OAuthFlow(
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ),
        session_factory=factory,
        base_url="http://localhost:8080",
        secrets_key=generate_key().encode(),
    )
    ctx = _Ctx(factory, flow)
    client = make_app(ctx)
    r = client.get("/oauth/connect/no-such-provider", follow_redirects=False)
    assert r.status_code == 404

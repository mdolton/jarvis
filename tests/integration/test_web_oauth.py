"""Web routes for OAuth connect/callback/disconnect (keyed on connection_id)."""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


class _Ctx:
    """Tiny stand-in for AppContext exposing only what oauth routes need."""

    def __init__(self, session_factory_, oauth_flow, catalog):
        self.session_factory = session_factory_
        self.oauth_flow = oauth_flow
        self.catalog = catalog
        self.mcp_manager = None  # set in tests that need it


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def _make_connection(factory, *, provider_key: str, runtime_name: str):
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key=provider_key, label="Default", runtime_name=runtime_name
        )
    return conn


def make_app(ctx) -> TestClient:
    app = create_app(app_context=ctx)
    return TestClient(app)


def make_flow(factory, handler, *, base_url="http://localhost:8080"):
    return OAuthFlow(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_factory=factory,
        base_url=base_url,
        secrets_key=generate_key().encode(),
        catalog=ProviderCatalog(factory),
    )


def make_ctx(factory, flow):
    return _Ctx(factory, flow, ProviderCatalog(factory))


def google_metadata():
    return {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
        "code_challenge_methods_supported": ["plain", "S256"],
    }


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

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")

    client = make_app(ctx)
    r = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://api.fastmail.com/oauth/authorize")


async def test_connect_unknown_connection_returns_404(factory):
    flow = make_flow(
        factory, lambda r: httpx.Response(404)
    )
    ctx = make_ctx(factory, flow)
    client = make_app(ctx)
    # Bad UUID -> 404.
    r = client.get("/oauth/connect/no-such-connection", follow_redirects=False)
    assert r.status_code == 404


async def test_connect_missing_connection_returns_404(factory):
    import uuid

    flow = make_flow(factory, lambda r: httpx.Response(404))
    ctx = make_ctx(factory, flow)
    client = make_app(ctx)
    # Valid UUID, but no such connection row.
    r = client.get(f"/oauth/connect/{uuid.uuid4()}", follow_redirects=False)
    assert r.status_code == 404


class _ManagerStub:
    def __init__(self):
        self.connected = []

    async def connect_connection(self, conn):
        self.connected.append(conn.runtime_name)


async def test_callback_happy_path_renders_success_and_swaps_server(factory):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        return httpx.Response(404)

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStub()
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")

    client = make_app(ctx)
    r = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    r2 = client.get(f"/oauth/callback?state={state}&code=abc")
    assert r2.status_code == 200
    assert "Connected" in r2.text
    assert ctx.mcp_manager.connected == ["fastmail:default"]


async def test_callback_unknown_state_renders_error(factory):
    flow = make_flow(factory, lambda r: httpx.Response(404))
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStub()
    client = make_app(ctx)
    r = client.get("/oauth/callback?state=bogus&code=zzz")
    assert r.status_code == 400
    assert "Authorization failed" in r.text


async def test_callback_with_error_param_renders_declined(factory):
    flow = make_flow(factory, lambda r: httpx.Response(404))
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStub()
    client = make_app(ctx)
    # state doesn't need to match a real pending row when error is set.
    r = client.get("/oauth/callback?error=access_denied&state=anything")
    assert r.status_code == 200
    assert "declined" in r.text.lower()


class _ManagerStubWithRemove(_ManagerStub):
    def __init__(self):
        super().__init__()
        self.removed = []

    async def remove_oauth_server(self, runtime_name):
        self.removed.append(runtime_name)


async def test_connect_gmail_redirects_to_google(factory, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")

    def handler(request):
        return httpx.Response(200, json=google_metadata())

    flow = make_flow(factory, handler, base_url="https://jarvis.example")
    ctx = make_ctx(factory, flow)
    client = make_app(ctx)

    # gmail is MANUAL mode — the client_id/secret must be on the connection.
    from jarvis.oauth.crypto import encrypt_blob

    key = flow._secrets_key
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="gmail",
            label="Default",
            runtime_name="gmail:default",
            client_id_enc=encrypt_blob(b"google-cid", key),
            client_secret_enc=encrypt_blob(b"google-secret", key),
        )

    resp = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    qs = parse_qs(urlparse(location).query)
    assert urlparse(location).netloc == "accounts.google.com"
    assert qs["client_id"] == ["google-cid"]
    assert qs["redirect_uri"] == ["https://jarvis.example/oauth/callback"]
    assert qs["access_type"] == ["offline"]


class _ManagerStubRemoveRaises(_ManagerStub):
    async def remove_oauth_server(self, runtime_name):
        raise RuntimeError("teardown boom")


class _ManagerStubConnectRaises(_ManagerStub):
    async def connect_connection(self, conn):
        raise RuntimeError("attach boom")


class _ManagerStubConnectHangs(_ManagerStub):
    async def connect_connection(self, conn):
        await asyncio.Event().wait()


async def test_disconnect_revokes_even_if_remove_fails(factory):
    """A failing/slow MCP teardown must not stop Disconnect from honoring the
    click. The connection tokens must be cleared regardless."""

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        if request.url.path == "/oauth/revoke":
            return httpx.Response(200)
        return httpx.Response(404)

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStubRemoveRaises()
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")

    client = make_app(ctx)
    r = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get(f"/oauth/callback?state={state}&code=abc")

    r2 = client.post(f"/oauth/disconnect/{conn.id}")
    assert r2.status_code in (200, 303)
    async with factory() as session:
        row = await MCPConnectionRepo(session).get(conn.id)
        assert row is not None
        assert row.access_token_enc is None
        assert row.status == "disconnected"


async def test_callback_marks_needs_reauth_when_attach_fails(factory):
    """If the MCP attach fails after token exchange, the connection must not be
    left showing 'connected' — it should be flagged needs_reauth so the
    dashboard tells the truth."""

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        return httpx.Response(404)

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStubConnectRaises()
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")

    client = make_app(ctx)
    r = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r2 = client.get(f"/oauth/callback?state={state}&code=abc")
    assert r2.status_code == 500
    async with factory() as session:
        row = await MCPConnectionRepo(session).get(conn.id)
        assert row is not None
        assert row.status == "needs_reauth"


async def test_callback_times_out_hung_mcp_attach(factory, monkeypatch):
    """A slow MCP attach must not hang the browser NOR mark the connection needs_reauth."""

    from jarvis.web.routes import oauth as oauth_routes

    monkeypatch.setattr(oauth_routes, "POST_CALLBACK_ATTACH_TIMEOUT", 0.01, raising=False)

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600},
            )
        return httpx.Response(404)

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStubConnectHangs()
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")
    app = create_app(app_context=ctx)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        r = await client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r2 = await asyncio.wait_for(
            client.get(f"/oauth/callback?state={state}&code=abc"),
            timeout=1.0,
        )

    assert r2.status_code == 200
    assert "still in progress" in r2.text
    async with factory() as session:
        row = await MCPConnectionRepo(session).get(conn.id)
        assert row is not None
        # Tokens were stored and the attach is merely pending — status must NOT
        # be downgraded to needs_reauth by a timeout.
        assert row.status == "connected"


async def test_disconnect_revokes_and_removes(factory):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        if request.url.path == "/oauth/revoke":
            return httpx.Response(200)
        return httpx.Response(404)

    flow = make_flow(factory, handler)
    ctx = make_ctx(factory, flow)
    ctx.mcp_manager = _ManagerStubWithRemove()
    conn = await _make_connection(factory, provider_key="fastmail", runtime_name="fastmail:default")

    client = make_app(ctx)
    r = client.get(f"/oauth/connect/{conn.id}", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get(f"/oauth/callback?state={state}&code=abc")

    r2 = client.post(f"/oauth/disconnect/{conn.id}")
    assert r2.status_code in (200, 303)
    assert ctx.mcp_manager.removed == ["fastmail:default"]
    async with factory() as session:
        row = await MCPConnectionRepo(session).get(conn.id)
        assert row is not None
        assert row.access_token_enc is None

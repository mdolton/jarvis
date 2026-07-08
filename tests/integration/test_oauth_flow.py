"""OAuthFlow tests using httpx.MockTransport (no real network).

The flow is keyed on connection_id: the service *definition* comes from the
ProviderCatalog, the *credentials/scopes/tokens* from the connection row.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jarvis.oauth.catalog import SEED_PROVIDERS, ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import decrypt_blob, encrypt_blob, generate_key
from jarvis.oauth.flow import OAuthCallbackError, OAuthDiscoveryError, OAuthFlow
from jarvis.oauth.store import MCPConnectionRepo, MCPPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest.fixture
def fastmail_metadata_payload():
    return {
        "issuer": "https://api.fastmail.com",
        "authorization_endpoint": "https://api.fastmail.com/oauth/authorize",
        "token_endpoint": "https://api.fastmail.com/oauth/token",
        "registration_endpoint": "https://api.fastmail.com/oauth/register",
        "revocation_endpoint": "https://api.fastmail.com/oauth/revoke",
        "code_challenge_methods_supported": ["S256"],
    }


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- discover / register: definition-only, no catalog/connection needed ------


async def test_discover_parses_metadata(fastmail_metadata_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-authorization-server"
        return httpx.Response(200, json=fastmail_metadata_payload)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(SEED_PROVIDERS["fastmail"])
    assert metadata.authorization_endpoint == "https://api.fastmail.com/oauth/authorize"
    assert metadata.registration_endpoint == "https://api.fastmail.com/oauth/register"
    assert "S256" in metadata.code_challenge_methods_supported


async def test_discover_rejects_missing_s256(fastmail_metadata_payload):
    fastmail_metadata_payload["code_challenge_methods_supported"] = ["plain"]
    def handler(request):
        return httpx.Response(200, json=fastmail_metadata_payload)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    with pytest.raises(OAuthDiscoveryError, match="S256"):
        await flow.discover(SEED_PROVIDERS["fastmail"])


async def test_discover_5xx_raises_discovery_error():
    def handler(request):
        return httpx.Response(503, text="upstream down")
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    with pytest.raises(OAuthDiscoveryError):
        await flow.discover(SEED_PROVIDERS["fastmail"])


async def test_register_client_dcr_returns_client_id(fastmail_metadata_payload):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".well-known/oauth-authorization-server"):
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            captured["body"] = request.read().decode()
            return httpx.Response(201, json={"client_id": "abc", "client_secret": "shh"})
        return httpx.Response(404)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(SEED_PROVIDERS["fastmail"])
    creds = await flow.register_client(SEED_PROVIDERS["fastmail"], metadata)
    assert creds.client_id == "abc"
    assert creds.client_secret == "shh"
    import json
    body = json.loads(captured["body"])
    assert body["redirect_uris"] == ["http://localhost:8080/oauth/callback"]
    assert "authorization_code" in body["grant_types"]


async def test_register_client_no_secret_returned_means_public(fastmail_metadata_payload):
    def handler(request):
        if request.url.path.endswith(".well-known/oauth-authorization-server"):
            return httpx.Response(200, json=fastmail_metadata_payload)
        return httpx.Response(201, json={"client_id": "pub-only"})
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(SEED_PROVIDERS["fastmail"])
    creds = await flow.register_client(SEED_PROVIDERS["fastmail"], metadata)
    assert creds.client_id == "pub-only"
    assert creds.client_secret is None


async def test_register_client_no_endpoint_raises(fastmail_metadata_payload):
    fastmail_metadata_payload.pop("registration_endpoint")
    def handler(request):
        return httpx.Response(200, json=fastmail_metadata_payload)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(SEED_PROVIDERS["fastmail"])
    from jarvis.oauth.flow import DCRUnsupportedError
    with pytest.raises(DCRUnsupportedError):
        await flow.register_client(SEED_PROVIDERS["fastmail"], metadata)


# --- connection-based flow ----------------------------------------------------


@pytest.fixture
async def db_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def _make_connection(factory, key, *, provider_key, runtime_name,
                           client_id=None, client_secret=None, scopes=None):
    async with factory() as s:
        return await MCPConnectionRepo(s).create(
            provider_key=provider_key, label=runtime_name, runtime_name=runtime_name,
            client_id_enc=encrypt_blob(client_id.encode(), key) if client_id else None,
            client_secret_enc=encrypt_blob(client_secret.encode(), key) if client_secret else None,
            scopes=scopes,
        )


async def test_start_authorization_first_time_registers_and_returns_consent_url(
    db_factory, fastmail_metadata_payload
):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid-1", "client_secret": "sec"})
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    # Fastmail is DCR: no client creds on the connection -> a client is registered.
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)

    parsed = urlparse(consent_url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "api.fastmail.com"
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cid-1"]
    assert qs["redirect_uri"] == ["http://localhost:8080/oauth/callback"]
    assert qs["code_challenge_method"] == ["S256"]
    state = qs["state"][0]

    # A pending row was inserted for the state, tied to this connection.
    async with db_factory() as session:
        pending = await MCPPendingRepo(session).get(state)
        assert pending is not None
        assert pending.connection_id == conn.id

    # The registered client_id was persisted back onto the connection (encrypted).
    async with db_factory() as session:
        stored = await MCPConnectionRepo(session).get(conn.id)
        assert stored.client_id_enc
        assert decrypt_blob(stored.client_id_enc, key) == b"cid-1"


async def test_start_authorization_skips_register_if_client_already_known(
    db_factory, fastmail_metadata_payload
):
    register_calls = {"count": 0}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            register_calls["count"] += 1
            return httpx.Response(201, json={"client_id": "x", "client_secret": "y"})
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    await flow.start_authorization(conn.id)
    # Second call reuses the client persisted onto the connection -> no re-register.
    await flow.start_authorization(conn.id)
    assert register_calls["count"] == 1


async def test_handle_callback_happy_path(db_factory, fastmail_metadata_payload):
    state_seen = {}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            state_seen["body"] = request.read().decode()
            return httpx.Response(200, json={
                "access_token": "AT", "refresh_token": "RT",
                "expires_in": 3600, "token_type": "Bearer",
                "scope": "mail.read",
            })
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]

    result = await flow.handle_callback(state=state, code="abc")
    assert result.provider_key == "fastmail"
    assert result.connection_id == conn.id
    assert result.runtime_name == "fastmail:main"

    # The pending row was deleted.
    async with db_factory() as session:
        assert await MCPPendingRepo(session).get(state) is None

    # Connection updated with real tokens.
    async with db_factory() as session:
        stored = await MCPConnectionRepo(session).get(conn.id)
        assert decrypt_blob(stored.access_token_enc, key) == b"AT"
        assert decrypt_blob(stored.refresh_token_enc, key) == b"RT"
        assert stored.scopes_granted == ["mail.read"]
        assert stored.status == "connected"


async def test_handle_callback_unknown_state_raises(db_factory):
    def handler(request):
        return httpx.Response(404)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=generate_key().encode(),
                     catalog=ProviderCatalog(db_factory))
    with pytest.raises(OAuthCallbackError, match="state"):
        await flow.handle_callback(state="not-a-real-state", code="abc")


async def test_refresh_happy_path(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            form = dict(p.split("=", 1) for p in request.read().decode().split("&"))
            if form["grant_type"] == "authorization_code":
                return httpx.Response(200, json={
                    "access_token": "AT", "refresh_token": "RT",
                    "expires_in": 3600, "token_type": "Bearer",
                })
            if form["grant_type"] == "refresh_token":
                return httpx.Response(200, json={
                    "access_token": "AT2", "refresh_token": "RT2",
                    "expires_in": 3600, "token_type": "Bearer",
                })
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=state, code="abc")

    new_headers = await flow.refresh(conn.id)
    assert new_headers["Authorization"] == "Bearer AT2"

    async with db_factory() as session:
        stored = await MCPConnectionRepo(session).get(conn.id)
        assert decrypt_blob(stored.access_token_enc, key) == b"AT2"
        assert decrypt_blob(stored.refresh_token_enc, key) == b"RT2"


async def test_refresh_invalid_grant_marks_needs_reauth(db_factory, fastmail_metadata_payload):
    state = {"step": "authcode"}
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            if state["step"] == "authcode":
                state["step"] = "refresh"
                return httpx.Response(200, json={
                    "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                })
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    from jarvis.oauth.flow import OAuthRefreshPermanentError
    with pytest.raises(OAuthRefreshPermanentError):
        await flow.refresh(conn.id)

    async with db_factory() as session:
        stored = await MCPConnectionRepo(session).get(conn.id)
        assert stored.status == "needs_reauth"
        assert "invalid_grant" in (stored.last_error or "")


async def test_revoke_calls_revocation_endpoint_without_mutating_row(
    db_factory, fastmail_metadata_payload
):
    revoke_calls = {"count": 0}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={
                "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            })
        if request.url.path == "/oauth/revoke":
            revoke_calls["count"] += 1
            return httpx.Response(200)
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    await flow.revoke(conn.id)
    assert revoke_calls["count"] >= 1
    # revoke() does NOT delete or clear the row — the caller handles that.
    async with db_factory() as session:
        assert await MCPConnectionRepo(session).get(conn.id) is not None


async def test_revoke_silent_when_endpoint_5xx(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        if request.url.path == "/oauth/revoke":
            return httpx.Response(503)
        return httpx.Response(404)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")
    # 5xx must not raise.
    await flow.revoke(conn.id)


@pytest.fixture
def google_metadata_payload():
    # Mirrors accounts.google.com/.well-known/openid-configuration: no registration_endpoint.
    return {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
        "code_challenge_methods_supported": ["plain", "S256"],
    }


async def test_discover_manual_mode_parses_google_metadata(google_metadata_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/openid-configuration"
        return httpx.Response(200, json=google_metadata_payload)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(SEED_PROVIDERS["gmail"])
    assert metadata.authorization_endpoint == "https://accounts.google.com/o/oauth2/v2/auth"
    assert metadata.token_endpoint == "https://oauth2.googleapis.com/token"
    assert metadata.registration_endpoint is None
    assert metadata.revocation_endpoint == "https://oauth2.googleapis.com/revoke"


async def test_start_authorization_manual_uses_connection_client(
    db_factory, google_metadata_payload
):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=google_metadata_payload)
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="https://jarvis.example/", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="gmail",
                                  runtime_name="gmail:main",
                                  client_id="google-cid", client_secret="google-secret",
                                  scopes=list(SEED_PROVIDERS["gmail"].default_scopes))
    consent_url = await flow.start_authorization(conn.id)

    parsed = urlparse(consent_url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert qs["client_id"] == ["google-cid"]
    assert qs["redirect_uri"] == ["https://jarvis.example/oauth/callback"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["scope"] == [
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose"
    ]


async def test_start_authorization_manual_missing_client_raises(
    db_factory, google_metadata_payload
):
    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    # Manual provider with no client creds on the connection.
    conn = await _make_connection(db_factory, key, provider_key="gmail",
                                  runtime_name="gmail:main")
    with pytest.raises(OAuthDiscoveryError, match="manual provider requires"):
        await flow.start_authorization(conn.id)


async def test_resource_indicator_omitted_when_disabled(
    db_factory, google_metadata_payload
):
    import dataclasses

    from jarvis.oauth import catalog as catalog_mod
    from jarvis.oauth.store import MCPProviderRepo

    # Flip send_resource_indicator off on the seeded gmail provider row.
    patched = dataclasses.replace(SEED_PROVIDERS["gmail"], send_resource_indicator=False)
    async with db_factory() as s:
        await MCPProviderRepo(s).upsert(
            key=patched.key, display_name=patched.display_name, kind=patched.kind,
            mcp_url=patched.mcp_url, builtin=True, auth_mode=patched.auth_mode.value,
            oauth_metadata_url=patched.oauth_metadata_url, pkce=patched.pkce,
            send_resource_indicator=False, extra_auth_params=dict(patched.extra_auth_params),
            default_scopes=list(patched.default_scopes), header_names=list(patched.header_names),
        )
    _ = catalog_mod  # keep import meaningful

    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="gmail",
                                  runtime_name="gmail:main",
                                  client_id="google-cid", client_secret="google-secret")
    consent_url = await flow.start_authorization(conn.id)
    qs = parse_qs(urlparse(consent_url).query)
    assert "resource" not in qs


async def test_resource_indicator_present_by_default(
    db_factory, google_metadata_payload
):
    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="gmail",
                                  runtime_name="gmail:main",
                                  client_id="google-cid", client_secret="google-secret")
    consent_url = await flow.start_authorization(conn.id)
    qs = parse_qs(urlparse(consent_url).query)
    assert qs["resource"] == ["https://gmailmcp.googleapis.com/mcp/v1"]


async def test_handle_callback_manual_uses_client_secret_basic(
    db_factory, google_metadata_payload
):
    """Gmail is a confidential client: token exchange must authenticate with
    client_secret_basic (Authorization: Basic base64(client_id:client_secret))
    and persist the refresh_token Google returns."""
    import base64

    seen = {}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=google_metadata_payload)
        if request.url.path == "/token":
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = request.read().decode()
            return httpx.Response(200, json={
                "access_token": "AT", "refresh_token": "RT",
                "expires_in": 3600, "token_type": "Bearer",
                "scope": "https://www.googleapis.com/auth/gmail.readonly",
            })
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="gmail",
                                  runtime_name="gmail:main",
                                  client_id="google-cid", client_secret="google-secret")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]

    result = await flow.handle_callback(state=state, code="abc")
    assert result.provider_key == "gmail"

    # Confidential client: Basic auth header, not client_id in the form body.
    expected_basic = base64.b64encode(b"google-cid:google-secret").decode()
    assert seen["auth"] == f"Basic {expected_basic}"
    assert "client_id=" not in seen["body"]

    # Tokens persisted (incl. the refresh_token the proactive scheduler relies on).
    async with db_factory() as session:
        stored = await MCPConnectionRepo(session).get(conn.id)
        assert decrypt_blob(stored.access_token_enc, key) == b"AT"
        assert decrypt_blob(stored.refresh_token_enc, key) == b"RT"


async def test_current_headers_returns_bearer(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        return httpx.Response(404)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    headers = await flow.current_headers(conn.id)
    assert headers["Authorization"] == "Bearer AT"


def _refresh_counting_handler(fastmail_metadata_payload, refresh_calls):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            form = dict(p.split("=", 1) for p in request.read().decode().split("&"))
            if form["grant_type"] == "authorization_code":
                return httpx.Response(
                    200,
                    json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600},
                )
            refresh_calls["count"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"AT{refresh_calls['count'] + 1}",
                    "refresh_token": f"RT{refresh_calls['count'] + 1}",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    return handler


async def test_concurrent_refresh_coalesces_to_one_exchange(db_factory, fastmail_metadata_payload):
    refresh_calls = {"count": 0}
    handler = _refresh_counting_handler(fastmail_metadata_payload, refresh_calls)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=state, code="abc")

    first, second = await asyncio.gather(flow.refresh(conn.id), flow.refresh(conn.id))

    assert refresh_calls["count"] == 1
    assert first == second == {"Authorization": "Bearer AT2"}


async def test_refresh_outside_window_hits_endpoint_again(db_factory, fastmail_metadata_payload):
    refresh_calls = {"count": 0}
    handler = _refresh_counting_handler(fastmail_metadata_payload, refresh_calls)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory),
                     refresh_coalesce_window_sec=0.0)
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=state, code="abc")

    assert await flow.refresh(conn.id) == {"Authorization": "Bearer AT2"}
    assert await flow.refresh(conn.id) == {"Authorization": "Bearer AT3"}
    assert refresh_calls["count"] == 2


async def test_handle_callback_rejects_expired_pending_state(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
            )
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(db_factory))
    conn = await _make_connection(db_factory, key, provider_key="fastmail",
                                  runtime_name="fastmail:main")
    consent_url = await flow.start_authorization(conn.id)
    state = parse_qs(urlparse(consent_url).query)["state"][0]

    # Backdate the pending row past the TTL.
    from sqlalchemy import update as sa_update

    from jarvis.persistence.models import MCPPendingRow

    async with db_factory() as session:
        await session.execute(
            sa_update(MCPPendingRow)
            .where(MCPPendingRow.state == state)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=700))
        )
        await session.commit()

    with pytest.raises(OAuthCallbackError, match="expired"):
        await flow.handle_callback(state=state, code="abc")

    async with db_factory() as session:
        assert await MCPPendingRepo(session).get(state) is None

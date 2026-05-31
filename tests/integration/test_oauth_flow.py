"""OAuthFlow tests using httpx.MockTransport (no real network)."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.crypto import generate_key
from jarvis.oauth.flow import OAuthDiscoveryError, OAuthFlow
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo
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


async def test_discover_parses_metadata(fastmail_metadata_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-authorization-server"
        return httpx.Response(200, json=fastmail_metadata_payload)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
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
        await flow.discover(OAUTH_CATALOG["fastmail"])


async def test_discover_5xx_raises_discovery_error():
    def handler(request):
        return httpx.Response(503, text="upstream down")
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    with pytest.raises(OAuthDiscoveryError):
        await flow.discover(OAUTH_CATALOG["fastmail"])


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
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
    creds = await flow.register_client(OAUTH_CATALOG["fastmail"], metadata)
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
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
    creds = await flow.register_client(OAUTH_CATALOG["fastmail"], metadata)
    assert creds.client_id == "pub-only"
    assert creds.client_secret is None


async def test_register_client_no_endpoint_raises(fastmail_metadata_payload):
    fastmail_metadata_payload.pop("registration_endpoint")
    def handler(request):
        return httpx.Response(200, json=fastmail_metadata_payload)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
    from jarvis.oauth.flow import DCRUnsupportedError
    with pytest.raises(DCRUnsupportedError):
        await flow.register_client(OAUTH_CATALOG["fastmail"], metadata)


@pytest.fixture
async def db_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


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
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")

    parsed = urlparse(consent_url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "api.fastmail.com"
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cid-1"]
    assert qs["redirect_uri"] == ["http://localhost:8080/oauth/callback"]
    assert qs["code_challenge_method"] == ["S256"]
    state = qs["state"][0]

    # An oauth_pending row was inserted for the state.
    async with db_factory() as session:
        pending = await OAuthPendingRepo(session).get(state)
        assert pending is not None
        assert pending.provider_key == "fastmail"

    # Credentials row exists with the registered client_id (encrypted).
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        # Pre-token-exchange row may exist with empty access_token; check client_id was stored.
        # If the impl defers credentials insert until token exchange, no row here.
        # Both designs are acceptable per spec — assert presence of either pending or registered state.
        assert cred is None or cred.client_id_enc != b""


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
                     base_url="http://localhost:8080", secrets_key=key)
    await flow.start_authorization("fastmail")
    await flow.start_authorization("fastmail")
    assert register_calls["count"] == 1


async def test_handle_callback_happy_path(db_factory, fastmail_metadata_payload):
    state_seen = {}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            body = request.read().decode()
            state_seen["body"] = body
            return httpx.Response(200, json={
                "access_token": "AT", "refresh_token": "RT",
                "expires_in": 3600, "token_type": "Bearer",
                "scope": "mail.read",
            })
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    state = parse_qs(urlparse(consent_url).query)["state"][0]

    result = await flow.handle_callback(state=state, code="abc")
    assert result.provider_key == "fastmail"

    # The pending row was deleted.
    async with db_factory() as session:
        assert await OAuthPendingRepo(session).get(state) is None

    # Credentials updated with real tokens.
    from jarvis.oauth.crypto import decrypt_blob
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert decrypt_blob(cred.access_token_enc, key) == b"AT"
        assert decrypt_blob(cred.refresh_token_enc, key) == b"RT"
        assert cred.scopes_granted == ["mail.read"]


async def test_handle_callback_unknown_state_raises(db_factory, fastmail_metadata_payload):
    def handler(request):
        return httpx.Response(404)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=generate_key().encode())
    from jarvis.oauth.flow import OAuthCallbackError
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
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=state, code="abc")

    new_headers = await flow.refresh("fastmail")
    assert new_headers["Authorization"] == "Bearer AT2"

    from jarvis.oauth.crypto import decrypt_blob
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert decrypt_blob(cred.access_token_enc, key) == b"AT2"
        assert decrypt_blob(cred.refresh_token_enc, key) == b"RT2"


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
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    from jarvis.oauth.flow import OAuthRefreshPermanentError
    with pytest.raises(OAuthRefreshPermanentError):
        await flow.refresh("fastmail")

    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert cred.status == "needs_reauth"
        assert "invalid_grant" in (cred.last_error or "")


async def test_revoke_calls_revocation_endpoint_and_deletes_credentials(
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
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    await flow.revoke("fastmail")
    assert revoke_calls["count"] >= 1
    async with db_factory() as session:
        assert await OAuthCredentialsRepo(session).get("fastmail") is None


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
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")
    # 5xx must not raise — local cleanup proceeds.
    await flow.revoke("fastmail")
    async with db_factory() as session:
        assert await OAuthCredentialsRepo(session).get("fastmail") is None


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
    metadata = await flow.discover(OAUTH_CATALOG["gmail"])
    assert metadata.authorization_endpoint == "https://accounts.google.com/o/oauth2/v2/auth"
    assert metadata.token_endpoint == "https://oauth2.googleapis.com/token"
    assert metadata.registration_endpoint is None
    assert metadata.revocation_endpoint == "https://oauth2.googleapis.com/revoke"


async def test_start_authorization_manual_seeds_client_from_env(
    db_factory, google_metadata_payload, monkeypatch
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=google_metadata_payload)
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="https://jarvis.example/", secrets_key=key)
    consent_url = await flow.start_authorization("gmail")

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

    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("gmail")
    assert cred is not None
    assert cred.client_id_enc != b""
    assert cred.access_token_enc == b""  # not yet authorized


async def test_start_authorization_manual_missing_env_raises(
    db_factory, google_metadata_payload, monkeypatch
):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)

    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    with pytest.raises(OAuthDiscoveryError, match="GOOGLE_OAUTH_CLIENT_ID"):
        await flow.start_authorization("gmail")


async def test_resource_indicator_omitted_when_disabled(
    db_factory, google_metadata_payload, monkeypatch
):
    import dataclasses

    from jarvis.oauth import catalog as catalog_mod

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    patched = dataclasses.replace(
        catalog_mod.OAUTH_CATALOG["gmail"], send_resource_indicator=False
    )
    monkeypatch.setitem(catalog_mod.OAUTH_CATALOG, "gmail", patched)

    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("gmail")
    qs = parse_qs(urlparse(consent_url).query)
    assert "resource" not in qs


async def test_resource_indicator_present_by_default(
    db_factory, google_metadata_payload, monkeypatch
):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")

    def handler(request):
        return httpx.Response(200, json=google_metadata_payload)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("gmail")
    qs = parse_qs(urlparse(consent_url).query)
    assert qs["resource"] == ["https://gmailmcp.googleapis.com/mcp/v1"]


async def test_handle_callback_manual_uses_client_secret_basic(
    db_factory, google_metadata_payload, monkeypatch
):
    """Gmail is a confidential client: token exchange must authenticate with
    client_secret_basic (Authorization: Basic base64(client_id:client_secret))
    and persist the refresh_token Google returns."""
    import base64

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "google-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
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
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("gmail")
    state = parse_qs(urlparse(consent_url).query)["state"][0]

    result = await flow.handle_callback(state=state, code="abc")
    assert result.provider_key == "gmail"

    # Confidential client: Basic auth header, not client_id in the form body.
    expected_basic = base64.b64encode(b"google-cid:google-secret").decode()
    assert seen["auth"] == f"Basic {expected_basic}"
    assert "client_id=" not in seen["body"]

    # Tokens persisted (incl. the refresh_token the proactive scheduler relies on).
    from jarvis.oauth.crypto import decrypt_blob
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("gmail")
        assert decrypt_blob(cred.access_token_enc, key) == b"AT"
        assert decrypt_blob(cred.refresh_token_enc, key) == b"RT"


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
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    headers = await flow.current_headers("fastmail")
    assert headers["Authorization"] == "Bearer AT"

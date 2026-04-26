"""OAuthFlow tests using httpx.MockTransport (no real network)."""

import httpx
import pytest

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.flow import OAuthDiscoveryError, OAuthFlow


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

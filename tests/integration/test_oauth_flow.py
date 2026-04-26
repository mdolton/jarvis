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

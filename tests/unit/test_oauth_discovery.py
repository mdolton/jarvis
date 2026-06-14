"""discover_provider tests using httpx.MockTransport (no real network)."""

import httpx
import pytest

from jarvis.oauth.discovery import discover_provider

AS_META_DCR = {
    "authorization_endpoint": "https://as.example.com/auth",
    "token_endpoint": "https://as.example.com/token",
    "registration_endpoint": "https://as.example.com/register",
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["read", "write"],
}
AS_META_MANUAL = {k: v for k, v in AS_META_DCR.items() if k != "registration_endpoint"}


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_prm_at_path_then_as_metadata_dcr():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource/mcp/v1":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp/v1", make_client(handler))
    assert r.oauth_metadata_url == "https://as.example.com/.well-known/oauth-authorization-server"
    assert r.auth_mode == "dcr"
    assert r.scopes_supported == ["read", "write"]


async def test_prm_at_origin_fallback():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp/v1", make_client(handler))
    assert r.auth_mode == "dcr"


async def test_www_authenticate_hint():
    def handler(req):
        p = req.url.path
        if p == "/mcp/v1":
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/prm-doc"'},
            )
        if p == "/prm-doc":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if req.url.host == "as.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp/v1", make_client(handler))
    assert r.oauth_metadata_url == "https://as.example.com/.well-known/oauth-authorization-server"


async def test_as_metadata_at_origin_fastmail_style():
    def handler(req):
        p = req.url.path
        if req.url.host == "api.fastmail.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://api.fastmail.com/mcp", make_client(handler))
    assert r.oauth_metadata_url == "https://api.fastmail.com/.well-known/oauth-authorization-server"
    assert r.auth_mode == "dcr"


async def test_oidc_only_document_manual():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://accounts.example.com"]})
        if req.url.host == "accounts.example.com":
            if p == "/.well-known/oauth-authorization-server":
                return httpx.Response(404)
            if p == "/.well-known/openid-configuration":
                return httpx.Response(200, json=AS_META_MANUAL)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/", make_client(handler))
    assert r.oauth_metadata_url == "https://accounts.example.com/.well-known/openid-configuration"
    assert r.auth_mode == "manual"


async def test_multiple_authorization_servers_first_used():
    def handler(req):
        p = req.url.path
        if p == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={"authorization_servers": ["https://as1.example.com", "https://as2.example.com"]},
            )
        if req.url.host == "as1.example.com" and p == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=AS_META_DCR)
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com", make_client(handler))
    assert r.authorization_servers == ["https://as1.example.com", "https://as2.example.com"]
    assert r.oauth_metadata_url == "https://as1.example.com/.well-known/oauth-authorization-server"


async def test_total_miss_returns_notes_no_metadata():
    def handler(req):
        return httpx.Response(404)

    r = await discover_provider("https://mcp.example.com/mcp", make_client(handler))
    assert r.oauth_metadata_url is None
    assert r.auth_mode is None
    assert r.notes  # non-empty trace


async def test_empty_mcp_url_raises():
    with pytest.raises(ValueError):
        await discover_provider("  ", make_client(lambda req: httpx.Response(404)))

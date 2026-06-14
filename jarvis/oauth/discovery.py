"""One-shot OAuth provider discovery for the Add Provider form.

Given only an MCP server URL, derive the authorization server's metadata URL,
whether it supports RFC 7591 DCR, and its advertised scopes — following the MCP
authorization discovery chain (RFC 9728 protected-resource metadata -> RFC 8414 /
OIDC authorization-server metadata). Pure: all HTTP goes through an injected
httpx.AsyncClient, so it is unit-testable with httpx.MockTransport.

Never raises for "couldn't find it" — returns a DiscoveryResult whose
oauth_metadata_url is None and whose `notes` explain what was tried. Raises only
on programmer error (empty mcp_url).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

_PRM_WELL_KNOWN = "/.well-known/oauth-protected-resource"
_AS_WELL_KNOWN = "/.well-known/oauth-authorization-server"
_OIDC_WELL_KNOWN = "/.well-known/openid-configuration"

# Matches resource_metadata="<url>" inside a WWW-Authenticate header.
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="?([^",\s]+)"?')


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    oauth_metadata_url: str | None = None
    auth_mode: str | None = None  # "dcr" | "manual" | None
    scopes_supported: list[str] = field(default_factory=list)
    authorization_servers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


async def _get_json(http: httpx.AsyncClient, url: str, notes: list[str]) -> dict | None:
    try:
        resp = await http.get(url)
    except httpx.HTTPError as e:
        notes.append(f"GET {url} failed: {e}")
        return None
    if resp.status_code >= 400:
        notes.append(f"GET {url} -> {resp.status_code}")
        return None
    try:
        return resp.json()
    except Exception:
        notes.append(f"GET {url} -> non-JSON body")
        return None


def _servers_from_prm(data: dict | None) -> list[str]:
    if not data:
        return []
    servers = data.get("authorization_servers")
    if isinstance(servers, list) and servers:
        return [str(s).rstrip("/") for s in servers]
    return []


async def _resource_metadata_hint(
    http: httpx.AsyncClient, mcp_url: str, notes: list[str]
) -> str | None:
    try:
        resp = await http.get(mcp_url)
    except httpx.HTTPError as e:
        notes.append(f"Unauthenticated GET {mcp_url} failed: {e}")
        return None
    match = _RESOURCE_METADATA_RE.search(resp.headers.get("WWW-Authenticate", ""))
    if match:
        notes.append("401 WWW-Authenticate advertised resource_metadata.")
        return match.group(1)
    return None


async def _find_authorization_servers(
    http: httpx.AsyncClient, mcp_url: str, notes: list[str]
) -> list[str]:
    origin = _origin(mcp_url)
    path = urlparse(mcp_url).path.rstrip("/")

    # 1 + 2. Protected-resource metadata, path-aware then at the origin.
    prm_candidates = []
    if path:
        prm_candidates.append(f"{origin}{_PRM_WELL_KNOWN}{path}")
    prm_candidates.append(f"{origin}{_PRM_WELL_KNOWN}")
    for prm_url in prm_candidates:
        servers = _servers_from_prm(await _get_json(http, prm_url, notes))
        if servers:
            notes.append(f"Found authorization_servers via PRM at {prm_url}.")
            return servers

    # 3. Unauthenticated request -> WWW-Authenticate resource_metadata hint.
    hint_url = await _resource_metadata_hint(http, mcp_url, notes)
    if hint_url:
        servers = _servers_from_prm(await _get_json(http, hint_url, notes))
        if servers:
            notes.append(f"Found authorization_servers via 401 hint {hint_url}.")
            return servers

    # 4. Assume the authorization server lives at the MCP origin (Fastmail-style).
    notes.append(f"Falling back to authorization-server metadata at origin {origin}.")
    return [origin]


async def _fetch_as_metadata(
    http: httpx.AsyncClient, base: str, notes: list[str]
) -> tuple[str | None, dict | None]:
    base = base.rstrip("/")
    for suffix in (_AS_WELL_KNOWN, _OIDC_WELL_KNOWN):
        url = f"{base}{suffix}"
        data = await _get_json(http, url, notes)
        if data and "authorization_endpoint" in data and "token_endpoint" in data:
            return url, data
    return None, None


async def discover_provider(mcp_url: str, http: httpx.AsyncClient) -> DiscoveryResult:
    mcp_url = mcp_url.strip()
    if not mcp_url:
        raise ValueError("mcp_url is required")
    notes: list[str] = []

    as_bases = await _find_authorization_servers(http, mcp_url, notes)

    for base in as_bases:
        meta_url, meta = await _fetch_as_metadata(http, base, notes)
        if meta is not None:
            auth_mode = "dcr" if meta.get("registration_endpoint") else "manual"
            scopes = [str(s) for s in (meta.get("scopes_supported") or [])]
            notes.append(f"Resolved metadata at {meta_url} (auth_mode={auth_mode}).")
            return DiscoveryResult(
                oauth_metadata_url=meta_url,
                auth_mode=auth_mode,
                scopes_supported=scopes,
                authorization_servers=as_bases,
                notes=notes,
            )

    notes.append("Found authorization server(s) but could not fetch valid metadata.")
    return DiscoveryResult(authorization_servers=as_bases, notes=notes)

"""Built-in registry of OAuth-capable MCP providers.

Adding a provider is a typed PR with tests, never a config edit.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class AuthMode(StrEnum):
    DCR = "dcr"          # RFC 7591 dynamic client registration
    MANUAL = "manual"    # operator-supplied client_id/secret (not implemented in v1)


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    key: str
    display_name: str
    mcp_url: str
    auth_mode: AuthMode
    oauth_metadata_url: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()
    pkce: bool = True


OAUTH_CATALOG: dict[str, ProviderEntry] = {
    "fastmail": ProviderEntry(
        key="fastmail",
        display_name="Fastmail",
        mcp_url="https://api.fastmail.com/mcp",
        auth_mode=AuthMode.DCR,
        oauth_metadata_url="https://api.fastmail.com/.well-known/oauth-authorization-server",
        scopes=(),
    ),
}


def assert_no_yaml_collision(yaml_server_names: Iterable[str]) -> None:
    """Raise ValueError if any YAML-defined MCP server name matches a catalog key."""
    catalog_keys = set(OAUTH_CATALOG)
    yaml_set = set(yaml_server_names)
    overlap = catalog_keys & yaml_set
    if overlap:
        joined = ", ".join(sorted(overlap))
        raise ValueError(
            f"YAML MCP server name(s) collide with built-in OAuth catalog keys: {joined}. "
            f"Rename the YAML server(s)."
        )

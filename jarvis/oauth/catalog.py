"""Built-in registry of OAuth-capable MCP providers.

Adding a provider is a typed PR with tests, never a config edit.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class AuthMode(StrEnum):
    DCR = "dcr"          # RFC 7591 dynamic client registration
    MANUAL = "manual"    # operator-supplied client_id/secret; DCR registration skipped


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
    # MANUAL mode: env vars holding operator-created client credentials.
    client_id_env: str | None = None
    client_secret_env: str | None = None
    # RFC 8707 resource indicator. Default on; flip off if a provider rejects it.
    send_resource_indicator: bool = True


OAUTH_CATALOG: dict[str, ProviderEntry] = {
    "fastmail": ProviderEntry(
        key="fastmail",
        display_name="Fastmail",
        mcp_url="https://api.fastmail.com/mcp",
        auth_mode=AuthMode.DCR,
        oauth_metadata_url="https://api.fastmail.com/.well-known/oauth-authorization-server",
        scopes=(),
    ),
    "gmail": ProviderEntry(
        key="gmail",
        display_name="Gmail",
        mcp_url="https://gmailmcp.googleapis.com/mcp/v1",
        auth_mode=AuthMode.MANUAL,
        oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ),
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        client_id_env="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
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

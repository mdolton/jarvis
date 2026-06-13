"""Built-in registry of OAuth-capable MCP providers.

Adding a provider is a typed PR with tests, never a config edit.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
    default_scopes: tuple[str, ...] = ()
    pkce: bool = True
    # MANUAL mode: env vars holding operator-created client credentials.
    client_id_env: str | None = None
    client_secret_env: str | None = None
    # RFC 8707 resource indicator. Default on; flip off if a provider rejects it.
    send_resource_indicator: bool = True
    kind: str = "oauth"
    header_names: tuple[str, ...] = ()


SEED_PROVIDERS: dict[str, ProviderEntry] = {
    "fastmail": ProviderEntry(
        key="fastmail",
        display_name="Fastmail",
        mcp_url="https://api.fastmail.com/mcp",
        auth_mode=AuthMode.DCR,
        oauth_metadata_url="https://api.fastmail.com/.well-known/oauth-authorization-server",
        kind="oauth",
        default_scopes=(),
    ),
    "gmail": ProviderEntry(
        key="gmail",
        display_name="Gmail",
        mcp_url="https://gmailmcp.googleapis.com/mcp/v1",
        auth_mode=AuthMode.MANUAL,
        oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        kind="oauth",
        default_scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ),
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        client_id_env="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
    ),
    "calendar": ProviderEntry(
        key="calendar",
        display_name="Google Calendar",
        mcp_url="https://calendarmcp.googleapis.com/mcp/v1",
        auth_mode=AuthMode.MANUAL,
        oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        kind="oauth",
        # Read-only scopes documented for the Calendar MCP server. See
        # developers.google.com/workspace/calendar/api/guides/configure-mcp-server
        default_scopes=(
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            "https://www.googleapis.com/auth/calendar.events.freebusy",
            "https://www.googleapis.com/auth/calendar.events.readonly",
        ),
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        # Reuses the same Google Cloud OAuth client as Gmail; one Web-application
        # client can request any scopes, so no separate credentials are needed.
        client_id_env="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
    ),
}


class ProviderCatalog:
    """Runtime, DB-backed view of the provider catalog. Reconstructs ProviderEntry rows."""
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    @staticmethod
    def _to_entry(row) -> ProviderEntry:
        return ProviderEntry(
            key=row.key, display_name=row.display_name, kind=row.kind, mcp_url=row.mcp_url,
            auth_mode=AuthMode(row.auth_mode) if row.auth_mode else AuthMode.DCR,
            oauth_metadata_url=row.oauth_metadata_url, pkce=row.pkce,
            send_resource_indicator=row.send_resource_indicator,
            extra_auth_params=dict(row.extra_auth_params or {}),
            default_scopes=tuple(row.default_scopes or ()),
            header_names=tuple(row.header_names or ()),
        )

    async def get(self, key: str) -> ProviderEntry:
        from jarvis.oauth.store import MCPProviderRepo
        async with self._factory() as s:
            row = await MCPProviderRepo(s).get(key)
        if row is None:
            raise KeyError(key)
        return self._to_entry(row)

    async def list(self) -> list[ProviderEntry]:
        from jarvis.oauth.store import MCPProviderRepo
        async with self._factory() as s:
            rows = await MCPProviderRepo(s).list_all()
        return [self._to_entry(r) for r in rows]


async def seed_built_in_providers(session) -> None:
    """Idempotently upsert SEED_PROVIDERS as builtin rows. Definition-only, no secrets."""
    from jarvis.oauth.store import MCPProviderRepo
    repo = MCPProviderRepo(session)
    for entry in SEED_PROVIDERS.values():
        await repo.upsert(
            key=entry.key, display_name=entry.display_name, kind=entry.kind,
            mcp_url=entry.mcp_url, builtin=True,
            auth_mode=entry.auth_mode.value, oauth_metadata_url=entry.oauth_metadata_url,
            pkce=entry.pkce, send_resource_indicator=entry.send_resource_indicator,
            extra_auth_params=dict(entry.extra_auth_params),
            default_scopes=list(entry.default_scopes), header_names=list(entry.header_names),
        )


def slug_label(label: str) -> str:
    """Stable connection slug: lowercase, non-alphanumeric runs -> single dash."""
    return re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")


def assert_no_yaml_collision(yaml_server_names: Iterable[str], catalog_keys: Iterable[str] | None = None) -> None:
    """Raise if any stdio YAML server name collides with a provider/catalog key."""
    keys = set(catalog_keys) if catalog_keys is not None else set(SEED_PROVIDERS)
    overlap = keys & set(yaml_server_names)
    if overlap:
        joined = ", ".join(sorted(overlap))
        raise ValueError(
            f"stdio MCP server name(s) collide with provider keys: {joined}. Rename the stdio server(s)."
        )

"""OAuth client used by the dashboard. State machine across discover, register,
authorize, exchange, refresh, revoke. All HTTP via injected httpx.AsyncClient
so unit tests can stub responses with MockTransport."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.oauth.catalog import AuthMode, ProviderEntry


class OAuthDiscoveryError(RuntimeError):
    """Raised when RFC 8414 metadata fetch or validation fails."""


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    code_challenge_methods_supported: list[str]


class OAuthFlow:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession] | None,
        base_url: str,
        secrets_key: bytes,
    ) -> None:
        self._http = http_client
        self._session_factory = session_factory
        self._base_url = base_url.rstrip("/")
        self._secrets_key = secrets_key

    @property
    def redirect_uri(self) -> str:
        return f"{self._base_url}/oauth/callback"

    async def discover(self, entry: ProviderEntry) -> ProviderMetadata:
        if entry.auth_mode is not AuthMode.DCR:
            raise NotImplementedError(
                f"Manual-mode OAuth not yet supported for provider {entry.key!r}"
            )
        if entry.oauth_metadata_url is None:
            raise OAuthDiscoveryError(f"{entry.key}: no oauth_metadata_url configured")
        try:
            resp = await self._http.get(entry.oauth_metadata_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise OAuthDiscoveryError(f"{entry.key}: metadata fetch failed: {e}") from e
        try:
            data = resp.json()
        except Exception as e:
            raise OAuthDiscoveryError(f"{entry.key}: metadata not JSON") from e
        try:
            metadata = ProviderMetadata(
                authorization_endpoint=data["authorization_endpoint"],
                token_endpoint=data["token_endpoint"],
                registration_endpoint=data.get("registration_endpoint"),
                revocation_endpoint=data.get("revocation_endpoint"),
                code_challenge_methods_supported=list(
                    data.get("code_challenge_methods_supported", [])
                ),
            )
        except KeyError as e:
            raise OAuthDiscoveryError(f"{entry.key}: metadata missing field {e}") from e
        if "S256" not in metadata.code_challenge_methods_supported:
            raise OAuthDiscoveryError(
                f"{entry.key}: provider does not advertise S256 PKCE method"
            )
        return metadata

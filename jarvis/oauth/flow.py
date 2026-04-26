"""OAuth client used by the dashboard. State machine across discover, register,
authorize, exchange, refresh, revoke. All HTTP via injected httpx.AsyncClient
so unit tests can stub responses with MockTransport."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.oauth.catalog import OAUTH_CATALOG, AuthMode, ProviderEntry
from jarvis.oauth.crypto import decrypt_blob, encrypt_blob
from jarvis.oauth.pkce import generate_code_challenge, generate_code_verifier, generate_state
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo


class OAuthDiscoveryError(RuntimeError):
    """Raised when RFC 8414 metadata fetch or validation fails."""


class DCRUnsupportedError(RuntimeError):
    """Provider doesn't advertise a registration_endpoint."""


@dataclass(frozen=True, slots=True)
class RegisteredClient:
    client_id: str
    client_secret: str | None


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

    async def start_authorization(self, provider_key: str) -> str:
        """Compose discover + register-if-needed + PKCE + state insert + URL build.

        Returns the provider's authorization URL that the user should be redirected to.

        Intermediate state: after this call, an oauth_credentials row exists with
        access_token_enc=b"" — a sentinel meaning "registered but not authorized."
        MCPManager bootstrap checks for this and skips SDK build until tokens land
        (i.e., until handle_callback exchanges the code and writes real tokens).
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for start_authorization")
        entry = OAUTH_CATALOG[provider_key]
        metadata = await self.discover(entry)

        # Get-or-register the DCR client.
        async with self._session_factory() as session:
            existing = await OAuthCredentialsRepo(session).get(provider_key)

        if existing is None or not existing.client_id_enc:
            client = await self.register_client(entry, metadata)
            # Persist client_id (and optional secret). access_token is empty until callback.
            async with self._session_factory() as session:
                cid_enc = encrypt_blob(client.client_id.encode(), self._secrets_key)
                sec_enc = (
                    encrypt_blob(client.client_secret.encode(), self._secrets_key)
                    if client.client_secret
                    else None
                )
                await OAuthCredentialsRepo(session).upsert(
                    provider_key=provider_key,
                    client_id_enc=cid_enc,
                    client_secret_enc=sec_enc,
                    # Sentinel: "registered but not authorized" — no real access token yet.
                    # bootstrap iteration in MCPManager will check access_token_enc == b""
                    # and skip building the SDK until handle_callback writes real tokens.
                    access_token_enc=b"",
                    refresh_token_enc=None,
                    token_expires_at=datetime.now(UTC),
                    scopes_granted=[],
                )
            client_id = client.client_id
        else:
            # Client already registered — skip DCR, recover client_id from encrypted storage.
            # A second call to start_authorization (e.g. user clicks Connect twice) reaches
            # this branch: we generate fresh PKCE/state and insert another pending row.
            # Both pending rows remain valid until their TTLs expire.
            client_id = decrypt_blob(existing.client_id_enc, self._secrets_key).decode()

        verifier = generate_code_verifier()
        challenge = generate_code_challenge(verifier)
        state = generate_state()

        async with self._session_factory() as session:
            await OAuthPendingRepo(session).insert(
                state=state,
                provider_key=provider_key,
                code_verifier=verifier,
                now=datetime.now(UTC),
            )

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if entry.scopes:
            params["scope"] = " ".join(entry.scopes)
        params.update(entry.extra_auth_params)
        return f"{metadata.authorization_endpoint}?{urlencode(params)}"

    async def register_client(
        self, entry: ProviderEntry, metadata: ProviderMetadata
    ) -> RegisteredClient:
        if metadata.registration_endpoint is None:
            raise DCRUnsupportedError(
                f"{entry.key}: provider does not support DCR; manual-mode OAuth not yet implemented"
            )
        body = {
            "client_name": "Jarvis",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "client_secret_basic",
        }
        resp = await self._http.post(metadata.registration_endpoint, json=body)
        resp.raise_for_status()
        data = resp.json()
        return RegisteredClient(
            client_id=data["client_id"],
            client_secret=data.get("client_secret"),
        )

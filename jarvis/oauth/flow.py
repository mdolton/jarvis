"""OAuth client used by the dashboard. State machine across discover, register,
authorize, exchange, refresh, revoke. All HTTP via injected httpx.AsyncClient
so unit tests can stub responses with MockTransport."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.oauth.catalog import OAUTH_CATALOG, ProviderEntry
from jarvis.oauth.crypto import decrypt_blob, encrypt_blob
from jarvis.oauth.pkce import generate_code_challenge, generate_code_verifier, generate_state
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo

_log = logging.getLogger(__name__)


class OAuthDiscoveryError(RuntimeError):
    """Raised when RFC 8414 metadata fetch or validation fails."""


class DCRUnsupportedError(RuntimeError):
    """Provider doesn't advertise a registration_endpoint."""


class OAuthCallbackError(RuntimeError):
    """Callback failed validation or token exchange."""


class OAuthRefreshTransientError(RuntimeError):
    """Transient refresh failure (network error or 5xx). Caller may retry."""


class OAuthRefreshPermanentError(RuntimeError):
    """Permanent refresh failure (invalid_grant or missing token). Requires re-auth."""


@dataclass(frozen=True, slots=True)
class RegisteredClient:
    client_id: str
    client_secret: str | None


@dataclass(frozen=True, slots=True)
class CallbackResult:
    provider_key: str
    scopes_granted: list[str]


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    code_challenge_methods_supported: list[str]
    scopes_supported: list[str]


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
                scopes_supported=list(data.get("scopes_supported", [])),
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
            # RFC 8707 + MCP authorization spec: identify the protected resource.
            "resource": entry.mcp_url,
        }
        # Effective scopes: catalog override if present, else everything the provider advertises.
        effective_scopes = list(entry.scopes) if entry.scopes else metadata.scopes_supported
        if effective_scopes:
            params["scope"] = " ".join(effective_scopes)
        params.update(entry.extra_auth_params)
        return f"{metadata.authorization_endpoint}?{urlencode(params)}"

    async def register_client(
        self, entry: ProviderEntry, metadata: ProviderMetadata
    ) -> RegisteredClient:
        if metadata.registration_endpoint is None:
            raise DCRUnsupportedError(
                f"{entry.key}: provider does not support DCR; manual-mode OAuth not yet implemented"
            )
        body: dict = {
            "client_name": "Jarvis",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        # Effective scopes: catalog override if present, else everything the provider advertises.
        effective_scopes = list(entry.scopes) if entry.scopes else metadata.scopes_supported
        if effective_scopes:
            body["scope"] = " ".join(effective_scopes)
        resp = await self._http.post(metadata.registration_endpoint, json=body)
        if resp.status_code >= 400:
            raise OAuthDiscoveryError(
                f"{entry.key}: DCR registration failed {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        return RegisteredClient(
            client_id=data["client_id"],
            client_secret=data.get("client_secret"),
        )

    async def handle_callback(self, *, state: str, code: str) -> CallbackResult:
        """Exchange authorization code for tokens and persist them.

        Looks up the pending row by state (CSRF defense), POSTs to the token
        endpoint, stores encrypted tokens, deletes the pending row, and returns
        a CallbackResult. Raises OAuthCallbackError for any validation or HTTP
        failure; on failure the pending row is left intact so the user can retry.
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for handle_callback")

        # --- CSRF defense: state MUST exist before we do anything else ---
        async with self._session_factory() as session:
            pending = await OAuthPendingRepo(session).get(state)
        if pending is None:
            raise OAuthCallbackError(f"unknown or expired state {state!r}")

        provider_key = pending.provider_key
        entry = OAUTH_CATALOG[provider_key]
        metadata = await self.discover(entry)

        # Recover registered client credentials from encrypted storage.
        async with self._session_factory() as session:
            cred = await OAuthCredentialsRepo(session).get(provider_key)
        if cred is None or not cred.client_id_enc:
            raise OAuthCallbackError(
                f"{provider_key}: no registered client; cannot complete callback"
            )

        client_id = decrypt_blob(cred.client_id_enc, self._secrets_key).decode()
        client_secret = (
            decrypt_blob(cred.client_secret_enc, self._secrets_key).decode()
            if cred.client_secret_enc
            else None
        )

        # Build the token exchange request.
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": pending.code_verifier,
            # RFC 8707 + MCP authorization spec: identify the protected resource.
            "resource": entry.mcp_url,
        }
        headers: dict[str, str] = {}
        if client_secret is not None:
            # client_secret_basic: Authorization: Basic base64(client_id:client_secret)
            basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            # Public client: send client_id in form body instead.
            form["client_id"] = client_id

        # POST to token endpoint. Failure does NOT delete the pending row.
        resp = await self._http.post(metadata.token_endpoint, data=form, headers=headers)
        if resp.status_code >= 400:
            raise OAuthCallbackError(
                f"token exchange returned {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise OAuthCallbackError(
                "token endpoint returned non-JSON response"
            ) from exc
        if "access_token" not in data:
            raise OAuthCallbackError(
                f"token response missing access_token: {str(data)[:300]}"
            )

        access_token: str = data["access_token"]
        refresh_token: str | None = data.get("refresh_token")
        expires_in = int(data.get("expires_in", 3600))
        scope: str = data.get("scope", "")
        scopes_granted = scope.split() if scope else []
        # Write-time uses the raw expires_in; 90s skew buffer is applied at refresh-time.
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        # Persist new tokens and delete the pending row atomically within the same session.
        async with self._session_factory() as session:
            await OAuthCredentialsRepo(session).upsert(
                provider_key=provider_key,
                client_id_enc=cred.client_id_enc,
                client_secret_enc=cred.client_secret_enc,
                access_token_enc=encrypt_blob(access_token.encode(), self._secrets_key),
                refresh_token_enc=(
                    encrypt_blob(refresh_token.encode(), self._secrets_key)
                    if refresh_token
                    else None
                ),
                token_expires_at=expires_at,
                scopes_granted=scopes_granted,
            )
            await OAuthPendingRepo(session).delete(state)

        return CallbackResult(provider_key=provider_key, scopes_granted=scopes_granted)

    async def refresh(self, provider_key: str) -> dict[str, str]:
        """Refresh tokens. Returns new headers dict for MCPServerStreamableHttp.

        Raises OAuthRefreshTransientError on network/5xx (caller may retry).
        Raises OAuthRefreshPermanentError on invalid_grant or missing refresh token
        (caller marks needs_reauth — already done here for the latter).
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for refresh")
        entry = OAUTH_CATALOG[provider_key]
        metadata = await self.discover(entry)

        async with self._session_factory() as session:
            cred = await OAuthCredentialsRepo(session).get(provider_key)
        if cred is None:
            raise OAuthRefreshPermanentError(f"{provider_key}: no credentials row")
        if not cred.refresh_token_enc:
            await self._mark_needs_reauth(provider_key, "no refresh_token on file")
            raise OAuthRefreshPermanentError(f"{provider_key}: no refresh_token on file")

        client_id = decrypt_blob(cred.client_id_enc, self._secrets_key).decode()
        client_secret = (
            decrypt_blob(cred.client_secret_enc, self._secrets_key).decode()
            if cred.client_secret_enc
            else None
        )
        refresh_token = decrypt_blob(cred.refresh_token_enc, self._secrets_key).decode()

        form: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            # RFC 8707 + MCP authorization spec: identify the protected resource.
            "resource": entry.mcp_url,
        }
        headers: dict[str, str] = {}
        if client_secret is not None:
            basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        else:
            form["client_id"] = client_id

        try:
            resp = await self._http.post(metadata.token_endpoint, data=form, headers=headers)
        except httpx.HTTPError as e:
            raise OAuthRefreshTransientError(f"network: {e}") from e

        if 500 <= resp.status_code < 600:
            raise OAuthRefreshTransientError(f"token endpoint {resp.status_code}")
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", "")
            except Exception:
                err = resp.text[:120]
            await self._mark_needs_reauth(provider_key, f"refresh failed: {err}")
            raise OAuthRefreshPermanentError(
                f"{provider_key}: refresh permanently failed: {err}"
            )

        data = resp.json()
        access_token: str = data["access_token"]
        new_refresh: str | None = data.get("refresh_token")  # may rotate
        expires_in = int(data.get("expires_in", 3600))
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        async with self._session_factory() as session:
            await OAuthCredentialsRepo(session).update_tokens(
                provider_key,
                access_token_enc=encrypt_blob(access_token.encode(), self._secrets_key),
                refresh_token_enc=(
                    encrypt_blob(new_refresh.encode(), self._secrets_key)
                    if new_refresh
                    else None
                ),
                token_expires_at=expires_at,
            )

        return {"Authorization": f"Bearer {access_token}"}

    async def revoke(self, provider_key: str) -> None:
        """Best-effort RFC 7009 token revocation followed by local credential deletion.

        Network errors and non-200 responses are logged but not raised — the user
        clicked Disconnect and we always honor that locally.
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for revoke")
        entry = OAUTH_CATALOG[provider_key]

        async with self._session_factory() as session:
            cred = await OAuthCredentialsRepo(session).get(provider_key)
        if cred is None:
            return  # nothing to revoke

        # Best-effort revocation against the provider.
        try:
            metadata = await self.discover(entry)
            if metadata.revocation_endpoint and cred.access_token_enc:
                access_token = decrypt_blob(cred.access_token_enc, self._secrets_key).decode()
                client_id = decrypt_blob(cred.client_id_enc, self._secrets_key).decode()
                client_secret = (
                    decrypt_blob(cred.client_secret_enc, self._secrets_key).decode()
                    if cred.client_secret_enc
                    else None
                )
                form: dict[str, str] = {
                    "token": access_token,
                    "token_type_hint": "access_token",
                }
                headers: dict[str, str] = {}
                if client_secret is not None:
                    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                    headers["Authorization"] = f"Basic {basic}"
                else:
                    form["client_id"] = client_id
                try:
                    resp = await self._http.post(
                        metadata.revocation_endpoint, data=form, headers=headers
                    )
                    if resp.status_code >= 400:
                        _log.warning(
                            "revocation endpoint returned %s for %s",
                            resp.status_code,
                            provider_key,
                        )
                except httpx.HTTPError as e:
                    _log.warning("revocation HTTP error for %s: %s", provider_key, e)
        except Exception:
            _log.exception(
                "revocation pre-step failed for %s; proceeding with local cleanup",
                provider_key,
            )

        # Always delete local credentials regardless of remote outcome.
        async with self._session_factory() as session:
            await OAuthCredentialsRepo(session).delete(provider_key)

    async def current_headers(self, provider_key: str) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <access_token>"}`` for an active provider.

        Does NOT refresh — that is the scheduler's responsibility.
        Raises ``LookupError`` if no credentials row exists or the access token is absent.
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for current_headers")
        async with self._session_factory() as session:
            cred = await OAuthCredentialsRepo(session).get(provider_key)
        if cred is None or not cred.access_token_enc:
            raise LookupError(f"{provider_key}: no active credentials")
        access_token = decrypt_blob(cred.access_token_enc, self._secrets_key).decode()
        return {"Authorization": f"Bearer {access_token}"}

    async def _mark_needs_reauth(self, provider_key: str, reason: str) -> None:
        async with self._session_factory() as session:
            await OAuthCredentialsRepo(session).set_status(
                provider_key, status="needs_reauth", last_error=reason
            )

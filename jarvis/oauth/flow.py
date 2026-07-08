"""OAuth client used by the dashboard. State machine across discover, register,
authorize, exchange, refresh, revoke. All HTTP via injected httpx.AsyncClient
so unit tests can stub responses with MockTransport."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.oauth.catalog import AuthMode, ProviderCatalog, ProviderEntry
from jarvis.oauth.crypto import decrypt_blob, encrypt_blob
from jarvis.oauth.pkce import generate_code_challenge, generate_code_verifier, generate_state
from jarvis.oauth.store import MCPConnectionRepo, MCPPendingRepo

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
    connection_id: UUID
    provider_key: str
    runtime_name: str
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
        catalog: ProviderCatalog | None = None,
        refresh_coalesce_window_sec: float = 30.0,
    ) -> None:
        self._http = http_client
        self._session_factory = session_factory
        self._base_url = base_url.rstrip("/")
        self._secrets_key = secrets_key
        self._catalog = catalog
        # Per-connection serialization of refresh() plus a recency window:
        # concurrent 401s coalesce onto one token exchange instead of burning a
        # single-use rotated refresh token (invalid_grant -> spurious
        # needs_reauth). A recency window (NOT a token_expires_at check) so a
        # server-side-revoked-but-unexpired token still reaches the endpoint
        # and produces the correct invalid_grant signal.
        self._refresh_window = refresh_coalesce_window_sec
        self._refresh_locks: dict[UUID, asyncio.Lock] = {}
        self._last_refresh: dict[UUID, tuple[float, dict[str, str]]] = {}

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

    async def start_authorization(self, connection_id: UUID) -> str:
        """Compose discover + register-if-needed + PKCE + state insert + URL build.

        Returns the provider's authorization URL that the user should be redirected to.
        The service *definition* (endpoints, scopes-default, auth_mode) comes from the
        ProviderCatalog; the *credentials* (client_id/secret) come from the connection
        row itself, so two connections on the same provider authorize independently.

        For MANUAL-mode providers the client credentials must already be present on the
        connection. For DCR providers a client is registered on first use and persisted
        back onto the connection. No placeholder credentials row is written — the
        connection row already exists; only a pending row is inserted.
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for start_authorization")
        if self._catalog is None:
            raise RuntimeError("OAuthFlow needs a catalog for start_authorization")

        async with self._session_factory() as session:
            conn = await MCPConnectionRepo(session).get(connection_id)
        if conn is None:
            raise OAuthDiscoveryError(f"unknown connection {connection_id}")

        entry = await self._catalog.get(conn.provider_key)
        metadata = await self.discover(entry)

        # Resolve client credentials from the CONNECTION.
        if not conn.client_id_enc:
            if entry.auth_mode is AuthMode.MANUAL:
                raise OAuthDiscoveryError(
                    f"{conn.provider_key}: manual provider requires "
                    "client_id/secret on the connection"
                )
            # DCR: register a client and persist it back onto the connection.
            client = await self.register_client(entry, metadata)
            async with self._session_factory() as session:
                await MCPConnectionRepo(session).set_client(
                    connection_id,
                    client_id_enc=encrypt_blob(client.client_id.encode(), self._secrets_key),
                    client_secret_enc=(
                        encrypt_blob(client.client_secret.encode(), self._secrets_key)
                        if client.client_secret
                        else None
                    ),
                )
            client_id = client.client_id
        else:
            client_id = decrypt_blob(conn.client_id_enc, self._secrets_key).decode()

        # Effective scopes: connection scopes if set, else everything the provider advertises.
        effective_scopes = list(conn.scopes) if conn.scopes else metadata.scopes_supported

        verifier = generate_code_verifier()
        challenge = generate_code_challenge(verifier)
        state = generate_state()

        async with self._session_factory() as session:
            await MCPPendingRepo(session).insert(
                state=state,
                connection_id=connection_id,
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
        if effective_scopes:
            params["scope"] = " ".join(effective_scopes)
        params.update(entry.extra_auth_params)
        # RFC 8707 + MCP authorization spec: identify the protected resource.
        if entry.send_resource_indicator:
            params["resource"] = entry.mcp_url
        return f"{metadata.authorization_endpoint}?{urlencode(params)}"

    async def register_client(
        self, entry: ProviderEntry, metadata: ProviderMetadata
    ) -> RegisteredClient:
        if metadata.registration_endpoint is None:
            raise DCRUnsupportedError(
                f"{entry.key}: provider does not support DCR; use auth_mode=MANUAL with client_id_env instead"
            )
        body: dict = {
            "client_name": "Jarvis",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        # Registration scope is advisory; the binding scopes go on the authorization
        # request. Use the provider default_scopes, falling back to what's advertised.
        reg_scopes = list(entry.default_scopes) or metadata.scopes_supported
        if reg_scopes:
            body["scope"] = " ".join(reg_scopes)
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
        if self._catalog is None:
            raise RuntimeError("OAuthFlow needs a catalog for handle_callback")

        # --- CSRF defense: state MUST exist before we do anything else ---
        async with self._session_factory() as session:
            pending = await MCPPendingRepo(session).get(state)
        if pending is None:
            raise OAuthCallbackError(f"unknown or expired state {state!r}")

        connection_id = pending.connection_id
        async with self._session_factory() as session:
            conn = await MCPConnectionRepo(session).get(connection_id)
        if conn is None or not conn.client_id_enc:
            raise OAuthCallbackError(
                f"{connection_id}: no registered client; cannot complete callback"
            )

        entry = await self._catalog.get(conn.provider_key)
        metadata = await self.discover(entry)

        # Recover registered client credentials from the connection.
        client_id = decrypt_blob(conn.client_id_enc, self._secrets_key).decode()
        client_secret = (
            decrypt_blob(conn.client_secret_enc, self._secrets_key).decode()
            if conn.client_secret_enc
            else None
        )

        # Build the token exchange request.
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": pending.code_verifier,
        }
        # RFC 8707 + MCP authorization spec: identify the protected resource.
        if entry.send_resource_indicator:
            form["resource"] = entry.mcp_url
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

        # Persist new tokens and delete the pending row.
        async with self._session_factory() as session:
            await MCPConnectionRepo(session).set_tokens(
                connection_id,
                access_token_enc=encrypt_blob(access_token.encode(), self._secrets_key),
                refresh_token_enc=(
                    encrypt_blob(refresh_token.encode(), self._secrets_key)
                    if refresh_token
                    else None
                ),
                token_expires_at=expires_at,
                scopes_granted=scopes_granted,
            )
            await MCPPendingRepo(session).delete(state)

        return CallbackResult(
            connection_id=connection_id,
            provider_key=conn.provider_key,
            runtime_name=conn.runtime_name,
            scopes_granted=scopes_granted,
        )

    async def refresh(self, connection_id: UUID) -> dict[str, str]:
        """Refresh tokens, serialized per connection with a recency window.

        Concurrent callers coalesce: whoever wins the lock does the exchange;
        callers that acquire the lock within ``refresh_coalesce_window_sec`` of
        a successful refresh get that refresh's headers without a second token
        exchange. Failed refreshes never populate the window.
        """
        lock = self._refresh_locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            recent = self._last_refresh.get(connection_id)
            if recent is not None and time.monotonic() - recent[0] < self._refresh_window:
                return dict(recent[1])
            headers = await self._refresh_locked(connection_id)
            self._last_refresh[connection_id] = (time.monotonic(), dict(headers))
            return dict(headers)

    async def _refresh_locked(self, connection_id: UUID) -> dict[str, str]:
        """Refresh tokens. Returns new headers dict for MCPServerStreamableHttp.

        Raises OAuthRefreshTransientError on network/5xx (caller may retry).
        Raises OAuthRefreshPermanentError on invalid_grant or missing refresh token
        (caller marks needs_reauth — already done here for the latter).
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for refresh")
        if self._catalog is None:
            raise RuntimeError("OAuthFlow needs a catalog for refresh")

        async with self._session_factory() as session:
            conn = await MCPConnectionRepo(session).get(connection_id)
        if conn is None:
            raise OAuthRefreshPermanentError(f"{connection_id}: no connection row")

        entry = await self._catalog.get(conn.provider_key)
        metadata = await self.discover(entry)

        if not conn.refresh_token_enc:
            await self._mark_needs_reauth(connection_id, "no refresh_token on file")
            raise OAuthRefreshPermanentError(f"{connection_id}: no refresh_token on file")

        client_id = decrypt_blob(conn.client_id_enc, self._secrets_key).decode()
        client_secret = (
            decrypt_blob(conn.client_secret_enc, self._secrets_key).decode()
            if conn.client_secret_enc
            else None
        )
        refresh_token = decrypt_blob(conn.refresh_token_enc, self._secrets_key).decode()

        form: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        # RFC 8707 + MCP authorization spec: identify the protected resource.
        if entry.send_resource_indicator:
            form["resource"] = entry.mcp_url
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
            await self._mark_needs_reauth(connection_id, f"refresh failed: {err}")
            raise OAuthRefreshPermanentError(
                f"{connection_id}: refresh permanently failed: {err}"
            )

        data = resp.json()
        access_token: str = data["access_token"]
        new_refresh: str | None = data.get("refresh_token")  # may rotate
        expires_in = int(data.get("expires_in", 3600))
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        async with self._session_factory() as session:
            await MCPConnectionRepo(session).update_tokens(
                connection_id,
                access_token_enc=encrypt_blob(access_token.encode(), self._secrets_key),
                refresh_token_enc=(
                    encrypt_blob(new_refresh.encode(), self._secrets_key)
                    if new_refresh
                    else None
                ),
                token_expires_at=expires_at,
            )

        return {"Authorization": f"Bearer {access_token}"}

    async def revoke(self, connection_id: UUID) -> None:
        """Best-effort RFC 7009 token revocation against the provider.

        Network errors and non-200 responses are logged but not raised. This does
        NOT mutate or delete the connection row — the caller is responsible for
        clearing/deleting it after revocation.
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for revoke")
        if self._catalog is None:
            raise RuntimeError("OAuthFlow needs a catalog for revoke")

        async with self._session_factory() as session:
            conn = await MCPConnectionRepo(session).get(connection_id)
        if conn is None:
            return  # nothing to revoke

        # Best-effort revocation against the provider.
        try:
            entry = await self._catalog.get(conn.provider_key)
            metadata = await self.discover(entry)
            if metadata.revocation_endpoint and conn.access_token_enc:
                access_token = decrypt_blob(conn.access_token_enc, self._secrets_key).decode()
                client_id = decrypt_blob(conn.client_id_enc, self._secrets_key).decode()
                client_secret = (
                    decrypt_blob(conn.client_secret_enc, self._secrets_key).decode()
                    if conn.client_secret_enc
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
                            connection_id,
                        )
                except httpx.HTTPError as e:
                    _log.warning("revocation HTTP error for %s: %s", connection_id, e)
        except Exception:
            _log.exception("revocation pre-step failed for %s", connection_id)

    async def current_headers(self, connection_id: UUID) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <access_token>"}`` for an active connection.

        Does NOT refresh — that is the scheduler's responsibility.
        Raises ``LookupError`` if no connection row exists or the access token is absent.
        """
        if self._session_factory is None:
            raise RuntimeError("OAuthFlow needs a session_factory for current_headers")
        async with self._session_factory() as session:
            conn = await MCPConnectionRepo(session).get(connection_id)
        if conn is None or not conn.access_token_enc:
            raise LookupError(f"{connection_id}: no active credentials")
        access_token = decrypt_blob(conn.access_token_enc, self._secrets_key).decode()
        return {"Authorization": f"Bearer {access_token}"}

    async def _mark_needs_reauth(self, connection_id: UUID, reason: str) -> None:
        async with self._session_factory() as session:
            await MCPConnectionRepo(session).set_status(
                connection_id, status="needs_reauth", last_error=reason
            )

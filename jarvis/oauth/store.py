"""Repositories for oauth_credentials and oauth_pending tables."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.persistence.models import MCPConnectionRow, MCPPendingRow, MCPProviderRow, OAuthCredentialsRow, OAuthPendingRow


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MCPProviderRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> MCPProviderRow | None:
        res = await self._session.execute(
            select(MCPProviderRow).where(MCPProviderRow.key == key)
        )
        return res.scalar_one_or_none()

    async def list_all(self) -> list[MCPProviderRow]:
        res = await self._session.execute(select(MCPProviderRow))
        return list(res.scalars())

    async def upsert(self, *, key: str, display_name: str, kind: str, mcp_url: str,
                     builtin: bool, auth_mode: str | None, oauth_metadata_url: str | None,
                     pkce: bool, send_resource_indicator: bool, extra_auth_params: dict,
                     default_scopes: list[str], header_names: list[str]) -> MCPProviderRow:
        now = _utcnow()
        row = await self.get(key)
        if row is None:
            row = MCPProviderRow(key=key, created_at=now)
            self._session.add(row)
        row.display_name = display_name; row.kind = kind; row.mcp_url = mcp_url
        row.builtin = builtin; row.auth_mode = auth_mode
        row.oauth_metadata_url = oauth_metadata_url; row.pkce = pkce
        row.send_resource_indicator = send_resource_indicator
        row.extra_auth_params = extra_auth_params; row.default_scopes = default_scopes
        row.header_names = header_names; row.updated_at = now
        await self._session.commit()
        return row

    async def delete(self, key: str) -> None:
        await self._session.execute(delete(MCPProviderRow).where(MCPProviderRow.key == key))
        await self._session.commit()


class MCPConnectionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, connection_id: UUID) -> MCPConnectionRow | None:
        return await self._session.get(MCPConnectionRow, connection_id)

    async def get_by_runtime_name(self, runtime_name: str) -> MCPConnectionRow | None:
        res = await self._session.execute(
            select(MCPConnectionRow).where(MCPConnectionRow.runtime_name == runtime_name)
        )
        return res.scalar_one_or_none()

    async def list_all(self) -> list[MCPConnectionRow]:
        res = await self._session.execute(select(MCPConnectionRow))
        return list(res.scalars())

    async def list_for_provider(self, provider_key: str) -> list[MCPConnectionRow]:
        res = await self._session.execute(
            select(MCPConnectionRow).where(MCPConnectionRow.provider_key == provider_key)
        )
        return list(res.scalars())

    async def list_enabled(self) -> list[MCPConnectionRow]:
        res = await self._session.execute(
            select(MCPConnectionRow).where(MCPConnectionRow.enabled.is_(True))
        )
        return list(res.scalars())

    async def create(self, *, provider_key: str, label: str, runtime_name: str,
                     client_id_enc: bytes | None = None, client_secret_enc: bytes | None = None,
                     scopes: list[str] | None = None, url_override: str | None = None,
                     headers_enc: bytes | None = None, enabled: bool = True) -> MCPConnectionRow:
        now = _utcnow()
        row = MCPConnectionRow(
            provider_key=provider_key, label=label, runtime_name=runtime_name, enabled=enabled,
            client_id_enc=client_id_enc, client_secret_enc=client_secret_enc,
            scopes=scopes or [], url_override=url_override, headers_enc=headers_enc,
            status="disconnected", created_at=now, updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def set_client(self, connection_id: UUID, *, client_id_enc: bytes,
                         client_secret_enc: bytes | None) -> None:
        row = await self.get(connection_id)
        if row is None:
            raise LookupError(connection_id)
        row.client_id_enc = client_id_enc
        row.client_secret_enc = client_secret_enc
        row.updated_at = _utcnow()
        await self._session.commit()

    async def set_tokens(self, connection_id: UUID, *, access_token_enc: bytes,
                         refresh_token_enc: bytes | None, token_expires_at: datetime,
                         scopes_granted: list[str]) -> None:
        row = await self.get(connection_id)
        if row is None:
            raise LookupError(connection_id)
        row.access_token_enc = access_token_enc
        if refresh_token_enc is not None:
            row.refresh_token_enc = refresh_token_enc
        row.token_expires_at = token_expires_at
        row.scopes_granted = scopes_granted
        row.status = "connected"
        row.last_error = None
        row.connected_at = row.connected_at or _utcnow()
        row.updated_at = _utcnow()
        await self._session.commit()

    async def update_tokens(self, connection_id: UUID, *, access_token_enc: bytes,
                            refresh_token_enc: bytes | None, token_expires_at: datetime) -> None:
        row = await self.get(connection_id)
        if row is None:
            raise LookupError(connection_id)
        row.access_token_enc = access_token_enc
        if refresh_token_enc is not None:
            row.refresh_token_enc = refresh_token_enc
        row.token_expires_at = token_expires_at
        row.status = "connected"
        row.last_error = None
        row.updated_at = _utcnow()
        await self._session.commit()

    async def set_status(self, connection_id: UUID, *, status: str, last_error: str | None) -> None:
        row = await self.get(connection_id)
        if row is None:
            return
        row.status = status
        row.last_error = last_error
        row.updated_at = _utcnow()
        await self._session.commit()

    async def set_enabled(self, connection_id: UUID, *, enabled: bool) -> None:
        row = await self.get(connection_id)
        if row is None:
            return
        row.enabled = enabled
        row.updated_at = _utcnow()
        await self._session.commit()

    async def clear_tokens(self, connection_id: UUID) -> None:
        """Disconnect: drop tokens, keep client + scopes so reconnect is one click."""
        row = await self.get(connection_id)
        if row is None:
            return
        row.access_token_enc = None
        row.refresh_token_enc = None
        row.token_expires_at = None
        row.scopes_granted = []
        row.status = "disconnected"
        row.last_error = None
        row.updated_at = _utcnow()
        await self._session.commit()

    async def delete(self, connection_id: UUID) -> None:
        await self._session.execute(
            delete(MCPConnectionRow).where(MCPConnectionRow.id == connection_id)
        )
        await self._session.commit()

    async def list_due_for_refresh(self, *, now: datetime, skew_seconds: int = 90
                                   ) -> list[MCPConnectionRow]:
        threshold = now + timedelta(seconds=skew_seconds)
        res = await self._session.execute(
            select(MCPConnectionRow).where(
                MCPConnectionRow.status == "connected",
                MCPConnectionRow.token_expires_at.is_not(None),
                MCPConnectionRow.token_expires_at <= threshold,
            )
        )
        return list(res.scalars())


class OAuthCredentialsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, provider_key: str) -> OAuthCredentialsRow | None:
        result = await self._session.execute(
            select(OAuthCredentialsRow).where(OAuthCredentialsRow.provider_key == provider_key)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        provider_key: str,
        client_id_enc: bytes,
        client_secret_enc: bytes | None,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        token_expires_at: datetime,
        scopes_granted: list[str],
    ) -> OAuthCredentialsRow:
        existing = await self.get(provider_key)
        now = _utcnow()
        if existing is None:
            row = OAuthCredentialsRow(
                provider_key=provider_key,
                client_id_enc=client_id_enc,
                client_secret_enc=client_secret_enc,
                access_token_enc=access_token_enc,
                refresh_token_enc=refresh_token_enc,
                token_expires_at=token_expires_at,
                scopes_granted=scopes_granted,
                status="connected",
                last_error=None,
                connected_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            existing.client_id_enc = client_id_enc
            existing.client_secret_enc = client_secret_enc
            existing.access_token_enc = access_token_enc
            existing.refresh_token_enc = refresh_token_enc
            existing.token_expires_at = token_expires_at
            existing.scopes_granted = scopes_granted
            existing.status = "connected"
            existing.last_error = None
            existing.updated_at = now
            row = existing
        await self._session.commit()
        return row

    async def set_status(self, provider_key: str, *, status: str, last_error: str | None) -> None:
        row = await self.get(provider_key)
        if row is None:
            return
        row.status = status
        row.last_error = last_error
        row.updated_at = _utcnow()
        await self._session.commit()

    async def update_tokens(
        self,
        provider_key: str,
        *,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        token_expires_at: datetime,
    ) -> None:
        row = await self.get(provider_key)
        if row is None:
            raise LookupError(f"no oauth_credentials row for {provider_key!r}")
        row.access_token_enc = access_token_enc
        if refresh_token_enc is not None:
            row.refresh_token_enc = refresh_token_enc
        row.token_expires_at = token_expires_at
        row.status = "connected"
        row.last_error = None
        row.updated_at = _utcnow()
        await self._session.commit()

    async def delete(self, provider_key: str) -> None:
        await self._session.execute(
            delete(OAuthCredentialsRow).where(OAuthCredentialsRow.provider_key == provider_key)
        )
        await self._session.commit()

    async def list_all(self) -> list[OAuthCredentialsRow]:
        result = await self._session.execute(select(OAuthCredentialsRow))
        return list(result.scalars())

    async def list_due_for_refresh(
        self, *, now: datetime, skew_seconds: int = 90
    ) -> list[OAuthCredentialsRow]:
        threshold = now + timedelta(seconds=skew_seconds)
        result = await self._session.execute(
            select(OAuthCredentialsRow).where(
                OAuthCredentialsRow.status == "connected",
                OAuthCredentialsRow.token_expires_at <= threshold,
            )
        )
        return list(result.scalars())


class OAuthPendingRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self, *, state: str, provider_key: str, code_verifier: str, now: datetime
    ) -> None:
        self._session.add(
            OAuthPendingRow(
                state=state,
                provider_key=provider_key,
                code_verifier=code_verifier,
                created_at=now,
            )
        )
        await self._session.commit()

    async def get(self, state: str) -> OAuthPendingRow | None:
        result = await self._session.execute(
            select(OAuthPendingRow).where(OAuthPendingRow.state == state)
        )
        return result.scalar_one_or_none()

    async def delete(self, state: str) -> None:
        await self._session.execute(
            delete(OAuthPendingRow).where(OAuthPendingRow.state == state)
        )
        await self._session.commit()

    async def sweep_expired(self, *, now: datetime, ttl_seconds: int = 600) -> int:
        cutoff = now - timedelta(seconds=ttl_seconds)
        result = await self._session.execute(
            delete(OAuthPendingRow).where(OAuthPendingRow.created_at < cutoff)
        )
        await self._session.commit()
        return result.rowcount or 0


class MCPPendingRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, *, state: str, connection_id: UUID, code_verifier: str,
                     now: datetime) -> None:
        self._session.add(MCPPendingRow(
            state=state, connection_id=connection_id, code_verifier=code_verifier, created_at=now,
        ))
        await self._session.commit()

    async def get(self, state: str) -> MCPPendingRow | None:
        res = await self._session.execute(
            select(MCPPendingRow).where(MCPPendingRow.state == state)
        )
        return res.scalar_one_or_none()

    async def delete(self, state: str) -> None:
        await self._session.execute(delete(MCPPendingRow).where(MCPPendingRow.state == state))
        await self._session.commit()

    async def sweep_expired(self, *, now: datetime, ttl_seconds: int = 600) -> int:
        cutoff = now - timedelta(seconds=ttl_seconds)
        res = await self._session.execute(
            delete(MCPPendingRow).where(MCPPendingRow.created_at < cutoff)
        )
        await self._session.commit()
        return res.rowcount or 0

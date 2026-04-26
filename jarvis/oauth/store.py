"""Repositories for oauth_credentials and oauth_pending tables."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.persistence.models import OAuthCredentialsRow, OAuthPendingRow


def _utcnow() -> datetime:
    return datetime.now(UTC)


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

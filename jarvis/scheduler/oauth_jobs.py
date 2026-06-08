"""APScheduler job functions for OAuth: proactive refresh and pending sweep."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.flow import (
    OAuthFlow,
    OAuthRefreshPermanentError,
    OAuthRefreshTransientError,
)
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo

_log = logging.getLogger(__name__)
OAUTH_REFRESH_ATTACH_TIMEOUT = 35.0


async def oauth_token_refresh(
    *,
    flow: OAuthFlow,
    mcp_manager,
    session_factory: async_sessionmaker[AsyncSession],
    skew_seconds: int = 90,
) -> None:
    """Refresh tokens that fall within the skew window. Swap or remove SDK servers."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = await OAuthCredentialsRepo(session).list_due_for_refresh(
            now=now, skew_seconds=skew_seconds
        )

    for cred in due:
        provider_key = cred.provider_key
        entry = OAUTH_CATALOG.get(provider_key)
        if entry is None:
            _log.warning("oauth refresh: unknown provider %r in DB; skipping", provider_key)
            continue
        try:
            new_headers = await flow.refresh(provider_key)
        except OAuthRefreshTransientError as e:
            _log.info("oauth refresh transient failure for %s: %s", provider_key, e)
            continue
        except OAuthRefreshPermanentError as e:
            _log.warning("oauth refresh permanent failure for %s: %s", provider_key, e)
            try:
                await mcp_manager.remove_oauth_server(provider_key)
            except Exception:
                _log.exception("failed to remove SDK server after needs_reauth")
            continue
        # Apply the refreshed token to the live connection in place. This cannot
        # fail and keeps the DB token state and the live socket in lockstep —
        # the previous rebuild-and-swap could fail and leave the connection
        # pinned to a dead token while the DB looked freshly refreshed. Only if
        # the provider isn't attached yet (no live holder) do we pay for a full
        # attach, which is the one path that can still hang, so keep it bounded.
        token = new_headers["Authorization"].removeprefix("Bearer ")
        if mcp_manager.update_oauth_token(provider_key, token):
            continue
        try:
            await asyncio.wait_for(
                mcp_manager.replace_oauth_server(
                    provider_key, url=entry.mcp_url, headers=new_headers
                ),
                timeout=OAUTH_REFRESH_ATTACH_TIMEOUT,
            )
        except Exception:
            _log.exception("failed to attach SDK server after refresh for %s", provider_key)


async def oauth_pending_sweep(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    ttl_seconds: int = 600,
) -> int:
    """Delete oauth_pending rows older than ttl_seconds. Returns number deleted."""
    async with session_factory() as session:
        return await OAuthPendingRepo(session).sweep_expired(
            now=datetime.now(UTC), ttl_seconds=ttl_seconds
        )

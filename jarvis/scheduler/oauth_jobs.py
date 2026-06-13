"""APScheduler job functions for OAuth: proactive refresh and pending sweep."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.oauth.flow import (
    OAuthFlow,
    OAuthRefreshPermanentError,
    OAuthRefreshTransientError,
)
from jarvis.oauth.store import MCPConnectionRepo, MCPPendingRepo

_log = logging.getLogger(__name__)
OAUTH_REFRESH_ATTACH_TIMEOUT = 35.0


async def oauth_token_refresh(
    *,
    flow: OAuthFlow,
    mcp_manager,
    session_factory: async_sessionmaker[AsyncSession],
    skew_seconds: int = 90,
) -> None:
    """Refresh tokens for connections within the skew window; apply in place or re-attach."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = await MCPConnectionRepo(session).list_due_for_refresh(now=now, skew_seconds=skew_seconds)

    for conn in due:
        try:
            new_headers = await flow.refresh(conn.id)
        except OAuthRefreshTransientError as e:
            _log.info("oauth refresh transient failure for %s: %s", conn.runtime_name, e)
            continue
        except OAuthRefreshPermanentError as e:
            _log.warning("oauth refresh permanent failure for %s: %s", conn.runtime_name, e)
            try:
                await mcp_manager.remove_oauth_server(conn.runtime_name)
            except Exception:
                _log.exception("failed to remove SDK server after needs_reauth")
            continue
        token = new_headers["Authorization"].removeprefix("Bearer ")
        if mcp_manager.update_oauth_token(conn.runtime_name, token):
            continue
        # Not attached yet — full attach (need the provider's url via the manager's catalog).
        try:
            entry = await mcp_manager._catalog.get(conn.provider_key)
            await asyncio.wait_for(
                mcp_manager.replace_oauth_server(
                    conn.runtime_name, url=conn.url_override or entry.mcp_url, headers=new_headers),
                timeout=OAUTH_REFRESH_ATTACH_TIMEOUT)
        except Exception:
            _log.exception("failed to attach SDK server after refresh for %s", conn.runtime_name)


async def oauth_pending_sweep(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    ttl_seconds: int = 600,
) -> int:
    """Delete mcp_pending rows older than ttl_seconds. Returns number deleted."""
    async with session_factory() as session:
        return await MCPPendingRepo(session).sweep_expired(
            now=datetime.now(UTC), ttl_seconds=ttl_seconds
        )

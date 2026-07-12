"""APScheduler job for auth housekeeping: prune dead sessions and login codes."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.persistence.repositories import AuthRepo

_log = logging.getLogger(__name__)


async def auth_session_cleanup(
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Delete expired sessions and dead auth codes. Returns rows removed."""
    async with session_factory() as session:
        removed = await AuthRepo(session).delete_expired_sessions_and_codes()
    if removed:
        _log.info("auth cleanup removed %d expired sessions/codes", removed)
    return removed

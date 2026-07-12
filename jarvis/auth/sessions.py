"""Dashboard session issuance, validation, rotation, and cookie handling.

Session tokens are opaque `secrets.token_urlsafe(32)` values; only their
SHA-256 hash is stored (compare-only secrets — see AuthRepo). The cookie is
the sole transport: EventSource cannot send custom headers, so /events/stream
in particular must ride the cookie.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from jarvis.config.schema import AuthConfig
from jarvis.persistence.models import UserRow
from jarvis.persistence.repositories import AuthRepo

# Write last_seen_at at most this often, so validation isn't a SQLite write
# on every request.
TOUCH_THROTTLE_SEC = 60


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class SessionManager:
    """Issue/validate/rotate dashboard sessions against the sessions table."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AuthConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config

    @property
    def cookie_name(self) -> str:
        # The __Host- prefix requires Secure + Path=/ + no Domain, so it can
        # never set over plain http; local dev (secure_cookies: false) drops
        # both the prefix and the Secure flag.
        return "__Host-jarvis_session" if self._config.secure_cookies else "jarvis_session"

    async def issue_session(self, user_id, request: Request | None = None) -> str:
        """Create a session for `user_id` and return the raw token (shown once)."""
        raw = secrets.token_urlsafe(32)
        async with self._session_factory() as session:
            await AuthRepo(session).create_session(
                user_id=user_id,
                token_hash=hash_token(raw),
                expires_at=datetime.now(UTC) + timedelta(days=self._config.session_ttl_days),
                user_agent=request.headers.get("user-agent") if request is not None else None,
                ip=request.client.host if request is not None and request.client else None,
            )
        return raw

    async def rotate_session(self, raw_token: str) -> str | None:
        """Swap the token of a live session for a fresh one; None if the session is gone.

        Rotate at every authentication event — OWASP session-management
        guidance: renewing the ID after (re)authentication defeats session
        fixation. (NIST does not require rotation; the citation is OWASP.)
        """
        new_raw = secrets.token_urlsafe(32)
        async with self._session_factory() as session:
            rotated = await AuthRepo(session).rotate_session_token(
                hash_token(raw_token), new_token_hash=hash_token(new_raw)
            )
        return new_raw if rotated else None

    async def validate(self, raw_token: str) -> UserRow | None:
        """Return the session's user if the session and user are live, else None.

        Live = not revoked, not past expires_at, last_seen_at within the idle
        timeout, and the user not disabled. Touches last_seen_at, throttled to
        once per TOUCH_THROTTLE_SEC.
        """
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            repo = AuthRepo(session)
            row = await repo.get_session_by_token_hash(hash_token(raw_token))
            if row is None or row.revoked_at is not None or row.expires_at <= now:
                return None
            idle_cutoff = now - timedelta(days=self._config.session_idle_timeout_days)
            if row.last_seen_at < idle_cutoff:
                return None
            user = await repo.get_user(row.user_id)
            if user is None or user.disabled_at is not None:
                return None
            if row.last_seen_at < now - timedelta(seconds=TOUCH_THROTTLE_SEC):
                await repo.touch_session(row.id)
            return user

    async def revoke(self, raw_token: str) -> None:
        """Revoke the session behind `raw_token` (logout)."""
        async with self._session_factory() as session:
            repo = AuthRepo(session)
            row = await repo.get_session_by_token_hash(hash_token(raw_token))
            if row is not None:
                await repo.revoke_session(row.id)

    def set_session_cookie(self, response: Response, raw_token: str) -> None:
        response.set_cookie(
            self.cookie_name,
            raw_token,
            max_age=self._config.session_ttl_days * 86400,
            path="/",
            secure=self._config.secure_cookies,
            httponly=True,
            # Lax, NOT Strict — this is a trap, not a preference: GET
            # /oauth/callback arrives as a CROSS-SITE TOP-LEVEL REDIRECT from
            # the external OAuth provider, and a Strict cookie is not sent on
            # that navigation, so the callback would appear logged-out and the
            # whole MCP OAuth flow would break.
            samesite="lax",
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.cookie_name,
            path="/",
            secure=self._config.secure_cookies,
            httponly=True,
            samesite="lax",
        )

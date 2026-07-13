"""Emailed one-time login codes: the enrollment and RECOVERY channel.

NIST SP 800-63B-4 §3.1.3.1: "Email SHALL NOT be used for out-of-band
authentication" — but the same section explicitly permits emailed
confirmation codes for ADDRESS VALIDATION and as RECOVERY codes. That is
exactly the role this module plays: the emailed code establishes/recovers
an account on the closed allow-list, and the passkey (registered from the
resulting session) is the authenticator for daily login.

It is a 6-DIGIT CODE, not a clickable link, on purpose: Outlook Safe Links,
corporate scanners and AV prefetchers GET links and burn single-use tokens
before the human ever clicks; a code has no URL to GET. (WorkOS deprecated
its magic-link API for exactly this and replaced it with a typed code.)

Enumeration resistance: request_login() does identical work whether or not
the email is on the allow-list — the allow-list check, DB writes, and the
mail send all happen OFF the request path (the caller backgrounds
issue_and_send). The mail send is the timing oracle; an early return on an
allow-list miss is how CVE-2026-26185 leaked a ~500ms delta behind a
perfectly generic message.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.auth.mailer import Mailer
from jarvis.config.schema import AuthConfig
from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.repositories import AuditRepo, AuthRepo

logger = logging.getLogger(__name__)

# Total guesses per code, counted across re-requests (replace_auth_code
# carries the counter over), so requesting a fresh code never refills the
# budget. 10^6 code space / 5 guesses ≈ 0.0005% per code lifetime.
MAX_VERIFY_ATTEMPTS = 5


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_code() -> str:
    """6 decimal digits from the CSPRNG, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


@dataclass(frozen=True)
class LoginRequest:
    """Everything the route needs after starting a login: the nonce for the
    same-browser cookie and the pre-hashed inputs for the background issue."""

    email: str
    code: str
    nonce: str
    ip: str | None


class LoginCodeService:
    """Issue and verify emailed login codes against the auth_codes table."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AuthConfig,
        mailer: Mailer,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._mailer = mailer

    def start_login(self, *, email: str, ip: str | None) -> LoginRequest:
        """Constant-work request-path half of a login: generate the code and
        the same-browser nonce. No allow-list check, no DB, no mail — those
        belong in issue_and_send, off the request path."""
        return LoginRequest(
            email=email.strip().lower(),
            code=generate_code(),
            nonce=secrets.token_urlsafe(32),
            ip=ip,
        )

    async def issue_and_send(self, request: LoginRequest) -> None:
        """Background half: allow-list gate, code row, mail send.

        Runs after the HTTP response is on the wire, so nothing here — not
        the allow-list miss, not a mail failure — can change what the
        requester sees or when they see it.
        """
        allowed = {addr.strip().lower() for addr in self._config.allowed_emails}
        if request.email not in allowed:
            logger.info("login code requested for non-allow-listed address; ignoring")
            return
        async with self._session_factory() as session:
            repo = AuthRepo(session)
            # Global in-flight cap: every live code is a guessable secret, so
            # the pool across all users stays bounded no matter how the
            # per-address/per-IP buckets are gamed. Off the request path like
            # everything else here, so the requester sees nothing.
            if await repo.count_active_auth_codes() >= self._config.max_inflight_codes:
                logger.warning(
                    "global in-flight login code cap (%d) reached; not issuing",
                    self._config.max_inflight_codes,
                )
                await AuditRepo(session).write_many(
                    [
                        AuditEvent(
                            type=AuditEventType.AUTH_RATE_LIMITED,
                            payload={
                                "scope": "global_inflight_codes",
                                "email": request.email,
                                "ip": request.ip,
                            },
                        )
                    ]
                )
                return
            user = await repo.get_or_create_user(request.email)
            if user.disabled_at is not None:
                logger.info("login code requested for disabled user; ignoring")
                return
            # Replaces (invalidates) any outstanding code and carries its
            # attempt counter over — a new code never resets the guess budget.
            await repo.replace_auth_code(
                user_id=user.id,
                code_hash=_sha256(request.code),
                nonce_hash=_sha256(request.nonce),
                expires_at=datetime.now(UTC) + timedelta(minutes=self._config.code_ttl_minutes),
                ip=request.ip,
            )
        try:
            await self._mailer.send(
                to=request.email,
                subject=f"{request.code} is your Jarvis sign-in code",
                text=(
                    f"Your Jarvis sign-in code is: {request.code}\n\n"
                    f"It expires in {self._config.code_ttl_minutes} minutes and only "
                    "works in the browser that requested it. If you didn't request "
                    "this, you can ignore this email."
                ),
            )
        except Exception as exc:
            # Audited, never surfaced: changing the user-facing response on a
            # send failure would reintroduce the enumeration oracle.
            logger.exception("login code mail send failed")
            async with self._session_factory() as session:
                await AuditRepo(session).write_many(
                    [
                        AuditEvent(
                            type=AuditEventType.AUTH_MAIL_SEND_FAILED,
                            payload={"to": request.email, "error": repr(exc)},
                        )
                    ]
                )

    async def verify(self, *, code: str, nonce: str | None) -> UUID | None:
        """Redeem a typed code; the requesting browser's nonce is mandatory.

        Every submission burns one of MAX_VERIFY_ATTEMPTS (tracked on the row,
        keyed by nonce so a stranger without the cookie can neither guess nor
        drain the real user's budget), and redemption is a single CAS — never
        SELECT-then-UPDATE — so concurrent submissions yield one winner.
        """
        if not nonce:
            return None
        nonce_hash = _sha256(nonce)
        async with self._session_factory() as session:
            repo = AuthRepo(session)
            attempts = await repo.record_code_attempt(nonce_hash)
            if attempts is None or attempts > MAX_VERIFY_ATTEMPTS:
                return None
            return await repo.consume_auth_code(
                _sha256(code.strip()),
                nonce_hash=nonce_hash,
                max_attempts=MAX_VERIFY_ATTEMPTS,
            )

"""WebAuthn passkey ceremonies: registration, login, and recovery codes.

py_webauthn (v3) does the cryptographic verification; everything stateful is
ours: challenges live server-side in webauthn_challenges (single-use, 5-minute
TTL, consumed via atomic CAS) and are looked up by row id at verification —
the challenge the authenticator signed is compared against the STORED bytes,
never against anything the client echoes back.

Registration is bound to an already-authenticated session: the challenge row
records the enrolling user and complete_registration refuses a mismatch. That
binding is what ties the passkey to the account established via the emailed
code. Credentials are created discoverable (resident_key=REQUIRED) with user
verification "preferred", so the login page's conditional UI can offer them
without knowing the user first.

rp_id/expected_origin come from AuthConfig and are NOT interchangeable:
rp_id is the registrable domain with no port; expected_origin is the full
scheme+host+port the browser asserts. Credentials are scoped to rp_id —
passkeys registered against localhost in dev will NOT work on the production
domain and must be re-registered there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from jarvis.config.schema import AuthConfig
from jarvis.persistence.models import UserRow, WebAuthnCredentialRow
from jarvis.persistence.repositories import AuthRepo

logger = logging.getLogger(__name__)

CHALLENGE_TTL_MINUTES = 5
RECOVERY_CODE_COUNT = 8

# Unambiguous lowercase alphabet (no 0/o, 1/l/i) for recovery codes;
# 10 chars ≈ 49 bits of entropy per code.
_RECOVERY_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


class PasskeyError(Exception):
    """A ceremony failed. The message is for logs; routes surface a generic
    failure so a probing client can't distinguish why."""


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Fresh recovery codes in display form (xxxxx-xxxxx)."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """Hash of the normalized code (case/dash/space-insensitive on entry)."""
    normalized = code.strip().lower().replace("-", "").replace(" ", "")
    return hashlib.sha256(normalized.encode()).hexdigest()


@dataclass(frozen=True)
class RegistrationResult:
    credential: WebAuthnCredentialRow
    # Set ONLY at first enrollment; displayed once and never recoverable
    # (only hashes are stored).
    recovery_codes: list[str] | None


class PasskeyService:
    """Server side of both WebAuthn ceremonies against the auth tables."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AuthConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config

    def _challenge_expiry(self) -> datetime:
        return datetime.now(UTC) + timedelta(minutes=CHALLENGE_TTL_MINUTES)

    # -- registration --------------------------------------------------

    async def begin_registration(self, user: UserRow) -> tuple[str, UUID]:
        """Options JSON for navigator.credentials.create() + the challenge id."""
        async with self._session_factory() as session:
            repo = AuthRepo(session)
            existing = await repo.list_credentials_for_user(user.id)
            options = generate_registration_options(
                rp_id=self._config.rp_id,
                rp_name=self._config.rp_name,
                # The user handle: random bytes fixed at account creation,
                # never the email (W3C WebAuthn §14.6.1 — authenticators can
                # surface it without user verification, so no PII).
                user_id=user.user_handle,
                user_name=user.email,
                exclude_credentials=[
                    PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
                    for c in existing
                ],
                authenticator_selection=AuthenticatorSelectionCriteria(
                    # Discoverable, so the login page's conditional UI can
                    # offer the passkey without asking who the user is.
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.PREFERRED,
                ),
            )
            row = await repo.create_challenge(
                kind="register",
                user_id=user.id,
                challenge=options.challenge,
                expires_at=self._challenge_expiry(),
            )
        return options_to_json(options), row.id

    async def complete_registration(
        self,
        user: UserRow,
        *,
        challenge_id: UUID,
        credential: str,
        name: str | None = None,
    ) -> RegistrationResult:
        """Verify the attestation and persist the credential.

        Raises PasskeyError on any failure: expired/reused/foreign challenge,
        verification failure, or duplicate credential id.
        """
        async with self._session_factory() as session:
            repo = AuthRepo(session)
            consumed = await repo.consume_challenge(challenge_id, kind="register")
            if consumed is None:
                raise PasskeyError("registration challenge missing, expired, or already used")
            challenge, bound_user_id = consumed
            if bound_user_id != user.id:
                raise PasskeyError("registration challenge bound to a different user")
            try:
                verification = verify_registration_response(
                    credential=credential,
                    expected_challenge=challenge,
                    expected_rp_id=self._config.rp_id,
                    expected_origin=self._config.expected_origin,
                    # user_verification is "preferred", not "required" — some
                    # roaming authenticators can't verify the user at all.
                    require_user_verification=False,
                )
            except Exception as exc:
                # Any parse or verification failure — malformed JSON/CBOR,
                # challenge/origin/rp_id mismatch, bad signature — is a reject.
                raise PasskeyError(f"registration verification failed: {exc}") from exc

            try:
                row = await repo.add_credential(
                    credential_id=bytes_to_base64url(verification.credential_id),
                    user_id=user.id,
                    public_key=verification.credential_public_key,
                    sign_count=verification.sign_count,
                    transports=_transports(credential),
                    aaguid=verification.aaguid,
                    backup_eligible=(
                        verification.credential_device_type == CredentialDeviceType.MULTI_DEVICE
                    ),
                    backup_state=verification.credential_backed_up,
                    name=name,
                )
            except IntegrityError as exc:
                raise PasskeyError("credential already registered") from exc

            # First enrollment mints the recovery codes: the backstop for
            # every-passkey-lost AND email-down. Shown once; hashes only.
            recovery: list[str] | None = None
            if not await repo.has_recovery_codes(user.id):
                recovery = generate_recovery_codes()
                await repo.create_recovery_codes(
                    user.id, [hash_recovery_code(code) for code in recovery]
                )
        return RegistrationResult(credential=row, recovery_codes=recovery)

    # -- login -----------------------------------------------------------

    async def begin_login(self) -> tuple[str, UUID]:
        """Options JSON for navigator.credentials.get() + the challenge id.

        No allow_credentials and no bound user: discoverable credentials
        answer with who they are, verified against the stored row.
        """
        options = generate_authentication_options(
            rp_id=self._config.rp_id,
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        async with self._session_factory() as session:
            row = await AuthRepo(session).create_challenge(
                kind="login",
                challenge=options.challenge,
                expires_at=self._challenge_expiry(),
            )
        return options_to_json(options), row.id

    async def complete_login(self, *, challenge_id: UUID, credential: str) -> UserRow:
        """Verify the assertion and return the credential's live user.

        Raises PasskeyError on any failure.
        """
        try:
            parsed = json.loads(credential)
            credential_id = parsed["id"]
            raw_user_handle = (parsed.get("response") or {}).get("userHandle")
        except (ValueError, TypeError, KeyError) as exc:
            raise PasskeyError("malformed authentication credential") from exc

        async with self._session_factory() as session:
            repo = AuthRepo(session)
            consumed = await repo.consume_challenge(challenge_id, kind="login")
            if consumed is None:
                raise PasskeyError("login challenge missing, expired, or already used")
            challenge, _ = consumed
            row = await repo.get_credential(credential_id)
            if row is None:
                raise PasskeyError("unknown credential")
            user = await repo.get_user(row.user_id)
            if user is None or user.disabled_at is not None:
                raise PasskeyError("credential belongs to a missing or disabled user")
            if raw_user_handle and base64url_to_bytes(raw_user_handle) != user.user_handle:
                raise PasskeyError("userHandle does not match the credential's user")
            try:
                verification = verify_authentication_response(
                    credential=credential,
                    expected_challenge=challenge,
                    expected_rp_id=self._config.rp_id,
                    expected_origin=self._config.expected_origin,
                    credential_public_key=row.public_key,
                    # 0, NOT row.sign_count — deliberate, do not "fix". Passing
                    # the stored count makes py_webauthn HARD-FAIL on any
                    # non-increasing counter, but synced passkeys (iCloud
                    # Keychain, Google Password Manager) legitimately report 0
                    # forever, and Google's passkey guidance deprecates
                    # relying on the counter — a hard fail bricks real logins.
                    # We do our own comparison below and only LOG regressions.
                    credential_current_sign_count=0,
                    require_user_verification=False,
                )
            except Exception as exc:
                raise PasskeyError(f"authentication verification failed: {exc}") from exc

            new_count = verification.new_sign_count
            if (row.sign_count > 0 or new_count > 0) and new_count <= row.sign_count:
                # Possible cloned authenticator (W3C §7.2 step 21) — but also
                # everyday behavior for synced passkeys, so log-only.
                logger.warning(
                    "passkey sign count did not increase (stored=%d, got=%d) "
                    "for credential %s of %s — possible cloned authenticator",
                    row.sign_count,
                    new_count,
                    row.credential_id[:12],
                    user.email,
                )
            await repo.record_credential_use(
                row.credential_id,
                sign_count=new_count,
                backup_state=verification.credential_backed_up,
            )
        return user


def _transports(credential: str) -> list | None:
    """The transports hint from the client response, if the browser sent one."""
    try:
        transports = json.loads(credential).get("response", {}).get("transports")
    except (ValueError, TypeError):
        return None
    if isinstance(transports, list) and all(isinstance(t, str) for t in transports):
        return transports
    return None

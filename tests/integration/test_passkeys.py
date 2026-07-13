"""WebAuthn passkey ceremonies end to end against real SQLite AND real
py_webauthn verification.

No mocking of the library: SoftAuthenticator below is a minimal software
authenticator (Ed25519 + hand-built CBOR attestation/assertion), so the tests
exercise genuine signature verification — origin and rp_id mismatches are
rejected by py_webauthn itself, not by a stub.
"""

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID

import cbor2
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from webauthn.helpers import bytes_to_base64url

from jarvis.auth.passkeys import PasskeyError, PasskeyService, hash_recovery_code
from jarvis.auth.sessions import SessionManager
from jarvis.config.schema import AuthConfig
from jarvis.persistence.db import Base
from jarvis.persistence.models import RecoveryCodeRow, WebAuthnChallengeRow
from jarvis.persistence.repositories import AuthRepo
from jarvis.web.app import create_app
from jarvis.web.auth_middleware import REGISTER_PATH
from jarvis.web.csrf import csrf_token_for_session

# rp_id/expected_origin left at defaults (localhost / http://localhost:8080);
# the soft authenticator signs for those unless a test says otherwise.
AUTH_ON = AuthConfig(enabled=True, secure_cookies=False, allowed_emails=["me@example.com"])
RP_ID = AUTH_ON.rp_id
ORIGIN = AUTH_ON.expected_origin
POST_HEADERS = {"origin": "http://testserver"}  # same-origin middleware


class SoftAuthenticator:
    """A software passkey: Ed25519 keypair + hand-assembled WebAuthn wire
    formats, verified by the real py_webauthn code paths."""

    def __init__(self, rp_id: str = RP_ID, origin: str = ORIGIN) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = Ed25519PrivateKey.generate()
        self.credential_id = secrets.token_bytes(32)
        self.user_handle: str | None = None  # base64url, captured at create()

    def _client_data(self, typ: str, challenge_b64u: str, origin: str) -> bytes:
        return json.dumps(
            {"type": typ, "challenge": challenge_b64u, "origin": origin, "crossOrigin": False}
        ).encode()

    def _cose_public_key(self) -> bytes:
        pub = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return cbor2.dumps({1: 1, 3: -8, -1: 6, -2: pub})  # OKP / EdDSA / Ed25519

    def create(self, options: dict, *, origin: str | None = None) -> dict:
        """A registration credential for these creation options."""
        self.user_handle = options["user"]["id"]
        client_data = self._client_data(
            "webauthn.create", options["challenge"], origin or self.origin
        )
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        attested = (
            bytes(16)  # zero aaguid
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_public_key()
        )
        auth_data = rp_hash + bytes([0x45]) + (0).to_bytes(4, "big") + attested  # UP|UV|AT
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(
                    cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
                ),
                "transports": ["internal"],
            },
        }

    def get(
        self,
        options: dict,
        *,
        sign_count: int = 1,
        origin: str | None = None,
        rp_id: str | None = None,
    ) -> dict:
        """An authentication assertion for these request options."""
        client_data = self._client_data("webauthn.get", options["challenge"], origin or self.origin)
        rp_hash = hashlib.sha256((rp_id or self.rp_id).encode()).digest()
        auth_data = rp_hash + bytes([0x05]) + sign_count.to_bytes(4, "big")  # UP|UV
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest())
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": self.user_handle,
            },
        }


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/passkeys.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _app(factory):
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config.jarvis.auth = AUTH_ON
    ctx.config.jarvis.timezone = "UTC"
    app = create_app(app_context=ctx)

    @app.get("/whoami")
    async def whoami(request: Request):  # a protected page behind the middleware
        return {"email": request.state.user.email}

    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _login_session(factory, email: str = "me@example.com") -> str:
    """User + session created directly (the emailed-code flow has its own tests)."""
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user(email)
    return await SessionManager(session_factory=factory, config=AUTH_ON).issue_session(user.id)


async def _sign_in(client, factory, email: str = "me@example.com") -> str:
    """Session cookie + matching CSRF header (unsafe methods demand both)."""
    raw = await _login_session(factory, email)
    client.cookies.set("jarvis_session", raw)
    client.headers["X-CSRF-Token"] = csrf_token_for_session(raw)
    return raw


async def _register(client, soft: SoftAuthenticator, name: str | None = None) -> httpx.Response:
    begin = (await client.post("/auth/passkey/register/begin", headers=POST_HEADERS)).json()
    return await client.post(
        "/auth/passkey/register/complete",
        json={
            "challenge_id": begin["challenge_id"],
            "credential": soft.create(begin["options"]),
            "name": name,
        },
        headers=POST_HEADERS,
    )


async def _enrolled_client(app, factory) -> tuple[httpx.AsyncClient, SoftAuthenticator]:
    """A signed-in client that has completed passkey enrollment."""
    client = _client(app)
    await _sign_in(client, factory)
    soft = SoftAuthenticator()
    resp = await _register(client, soft)
    assert resp.status_code == 200, resp.text
    return client, soft


# -- registration ------------------------------------------------------


async def test_registration_requires_authenticated_session(factory):
    """No session, no passkey: registration from outside a session is 401."""
    async with _client(_app(factory)) as client:
        begin = await client.post("/auth/passkey/register/begin", headers=POST_HEADERS)
        complete = await client.post(
            "/auth/passkey/register/complete",
            json={"challenge_id": "0" * 32, "credential": {}},
            headers=POST_HEADERS,
        )
    assert begin.status_code == 401
    assert complete.status_code == 401


async def test_registration_ceremony_end_to_end(factory):
    app = _app(factory)
    async with _client(app) as client:
        await _sign_in(client, factory)

        begin = await client.post("/auth/passkey/register/begin", headers=POST_HEADERS)
        assert begin.status_code == 200
        options = begin.json()["options"]
        # Discoverable credential, UV preferred, correct rp — the contract
        # the conditional-UI login depends on.
        assert options["rp"]["id"] == RP_ID
        assert options["authenticatorSelection"]["residentKey"] == "required"
        assert options["authenticatorSelection"]["userVerification"] == "preferred"

        soft = SoftAuthenticator()
        resp = await _register(client, soft, name="test key")
        assert resp.status_code == 200
        body = resp.json()
        assert body["verified"] is True
        assert body["credential_id"] == bytes_to_base64url(soft.credential_id)

        # Enrolled: the dashboard is reachable now.
        assert (await client.get("/whoami")).status_code == 200

    async with factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        # The user handle offered to the authenticator is the random bytes
        # handle, never the email (W3C §14.6.1: no PII in user.id).
        assert options["user"]["id"] == bytes_to_base64url(user.user_handle)
        rows = await repo.list_credentials_for_user(user.id)
    assert len(rows) == 1
    assert rows[0].name == "test key"
    assert rows[0].transports == ["internal"]


async def test_first_enrollment_mints_recovery_codes_shown_once_stored_hashed(factory):
    app = _app(factory)
    async with _client(app) as client:
        await _sign_in(client, factory)
        first = await _register(client, SoftAuthenticator())
        second = await _register(client, SoftAuthenticator())

    codes = first.json()["recovery_codes"]
    assert isinstance(codes, list) and len(codes) == 8
    # A second passkey never re-mints them — display is strictly once.
    assert second.json()["recovery_codes"] is None

    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
        rows = (
            (
                await session.execute(
                    select(RecoveryCodeRow).where(RecoveryCodeRow.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 8
    stored = {row.code_hash for row in rows}
    assert stored == {hash_recovery_code(code) for code in codes}
    # Only hashes at rest — no stored value equals a displayed code.
    assert not (stored & set(codes))


async def test_registration_challenge_bound_to_enrolling_user(factory):
    """A challenge issued to one user cannot complete another's registration."""
    service = PasskeyService(session_factory=factory, config=AUTH_ON)
    async with factory() as session:
        repo = AuthRepo(session)
        alice = await repo.get_or_create_user("me@example.com")
        mallory = await repo.get_or_create_user("mallory@example.com")

    options_json, challenge_id = await service.begin_registration(alice)
    credential = json.dumps(SoftAuthenticator().create(json.loads(options_json)))
    with pytest.raises(PasskeyError, match="different user"):
        await service.complete_registration(
            mallory, challenge_id=challenge_id, credential=credential
        )


# -- login -------------------------------------------------------------


async def test_login_ceremony_end_to_end(factory):
    app = _app(factory)
    client, soft = await _enrolled_client(app, factory)
    await client.aclose()

    # A brand-new browser: no session, only the passkey.
    async with _client(app) as fresh:
        begin = await fresh.post("/auth/passkey/login/begin", headers=POST_HEADERS)
        assert begin.status_code == 200
        options = begin.json()["options"]
        assert options.get("allowCredentials", []) == []  # discoverable flow

        complete = await fresh.post(
            "/auth/passkey/login/complete",
            json={
                "challenge_id": begin.json()["challenge_id"],
                "credential": soft.get(options, sign_count=1),
            },
            headers=POST_HEADERS,
        )
        assert complete.status_code == 200
        assert complete.json() == {"verified": True, "redirect": "/"}
        assert "jarvis_session" in complete.cookies

        whoami = await fresh.get("/whoami")
    assert whoami.status_code == 200
    assert whoami.json() == {"email": "me@example.com"}

    async with factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        row = (await repo.list_credentials_for_user(user.id))[0]
    assert row.sign_count == 1
    assert row.last_used_at is not None


async def _login_attempt(client, soft: SoftAuthenticator, **soft_kwargs) -> httpx.Response:
    begin = (await client.post("/auth/passkey/login/begin", headers=POST_HEADERS)).json()
    resp = await client.post(
        "/auth/passkey/login/complete",
        json={
            "challenge_id": begin["challenge_id"],
            "credential": soft.get(begin["options"], **soft_kwargs),
        },
        headers=POST_HEADERS,
    )
    # Success issues a NEW session; refresh the CSRF header to match (the
    # browser equivalent: the post-login navigation re-renders the token).
    new_raw = resp.cookies.get("jarvis_session")
    if new_raw:
        client.headers["X-CSRF-Token"] = csrf_token_for_session(new_raw)
    return resp


async def test_login_challenge_is_single_use(factory):
    app = _app(factory)
    client, soft = await _enrolled_client(app, factory)
    await client.aclose()

    async with _client(app) as fresh:
        begin = (await fresh.post("/auth/passkey/login/begin", headers=POST_HEADERS)).json()
        payload = {
            "challenge_id": begin["challenge_id"],
            "credential": soft.get(begin["options"]),
        }
        first = await fresh.post("/auth/passkey/login/complete", json=payload, headers=POST_HEADERS)
        fresh.cookies.clear()
        replay = await fresh.post(
            "/auth/passkey/login/complete", json=payload, headers=POST_HEADERS
        )
    assert first.status_code == 200
    assert replay.status_code == 401
    assert "jarvis_session" not in replay.cookies


async def test_registration_challenge_is_single_use(factory):
    app = _app(factory)
    async with _client(app) as client:
        await _sign_in(client, factory)
        begin = (await client.post("/auth/passkey/register/begin", headers=POST_HEADERS)).json()
        payload = {
            "challenge_id": begin["challenge_id"],
            "credential": SoftAuthenticator().create(begin["options"]),
        }
        first = await client.post(
            "/auth/passkey/register/complete", json=payload, headers=POST_HEADERS
        )
        replay = await client.post(
            "/auth/passkey/register/complete", json=payload, headers=POST_HEADERS
        )
    assert first.status_code == 200
    assert replay.status_code == 400


async def test_expired_challenge_rejected(factory):
    app = _app(factory)
    async with _client(app) as client:
        await _sign_in(client, factory)
        begin = (await client.post("/auth/passkey/register/begin", headers=POST_HEADERS)).json()

        async with factory() as session:  # the 5-minute TTL elapses
            await session.execute(
                update(WebAuthnChallengeRow)
                .where(WebAuthnChallengeRow.id == UUID(begin["challenge_id"]))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        resp = await client.post(
            "/auth/passkey/register/complete",
            json={
                "challenge_id": begin["challenge_id"],
                "credential": SoftAuthenticator().create(begin["options"]),
            },
            headers=POST_HEADERS,
        )
    assert resp.status_code == 400


async def test_origin_mismatch_rejected_by_real_verification(factory):
    app = _app(factory)
    client, soft = await _enrolled_client(app, factory)

    # Registration attestation minted for a foreign origin.
    begin = (await client.post("/auth/passkey/register/begin", headers=POST_HEADERS)).json()
    reg = await client.post(
        "/auth/passkey/register/complete",
        json={
            "challenge_id": begin["challenge_id"],
            "credential": SoftAuthenticator().create(begin["options"], origin="http://evil:8080"),
        },
        headers=POST_HEADERS,
    )
    assert reg.status_code == 400

    # Login assertion whose clientDataJSON claims a foreign origin.
    login = await _login_attempt(client, soft, origin="https://evil.example")
    await client.aclose()
    assert login.status_code == 401
    assert "not accepted" not in login.text  # generic passkey message, no detail
    assert login.json()["verified"] is False


async def test_rp_id_mismatch_rejected_by_real_verification(factory):
    app = _app(factory)
    client, soft = await _enrolled_client(app, factory)
    login = await _login_attempt(client, soft, rp_id="evil.example")
    await client.aclose()
    assert login.status_code == 401


async def test_sign_count_regression_logged_but_not_fatal(factory, caplog):
    """Synced passkeys legitimately repeat/zero the counter (Google deprecates
    relying on it) — a regression must log a warning, never block the login."""
    app = _app(factory)
    client, soft = await _enrolled_client(app, factory)

    assert (await _login_attempt(client, soft, sign_count=5)).status_code == 200
    with caplog.at_level(logging.WARNING, logger="jarvis.auth.passkeys"):
        regressed = await _login_attempt(client, soft, sign_count=3)
    await client.aclose()

    assert regressed.status_code == 200  # NOT fatal
    assert regressed.json()["verified"] is True
    assert "sign count did not increase" in caplog.text
    assert "stored=5, got=3" in caplog.text

    async with factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        row = (await repo.list_credentials_for_user(user.id))[0]
    assert row.sign_count == 3  # updated to the reported value (W3C §7.2/21)


async def test_unknown_credential_rejected(factory):
    async with _client(_app(factory)) as client:
        resp = await _login_attempt(client, SoftAuthenticator())  # never registered
    assert resp.status_code == 401


# -- mandatory enrollment ----------------------------------------------


async def test_zero_credential_user_is_forced_to_enrollment(factory):
    """Emailed-code-only must never be a steady state (NIST SP 800-63B
    §3.1.3.1): with no passkey every page redirects to enrollment."""
    app = _app(factory)
    async with _client(app) as client:
        await _sign_in(client, factory)

        page = await client.get("/whoami")
        assert page.status_code == 302
        assert page.headers["location"] == REGISTER_PATH

        htmx = await client.get("/whoami", headers={"HX-Request": "true"})
        assert htmx.status_code == 401
        assert htmx.headers["HX-Redirect"] == REGISTER_PATH

        # The enrollment page itself (and its ceremony routes) are reachable.
        assert (await client.get(REGISTER_PATH)).status_code == 200

        # Enrolling lifts the gate.
        assert (await _register(client, SoftAuthenticator())).status_code == 200
        assert (await client.get("/whoami")).status_code == 200


async def test_enrollment_page_requires_session(factory):
    async with _client(_app(factory)) as client:
        resp = await client.get(REGISTER_PATH)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/login"


# -- management --------------------------------------------------------


async def test_passkey_settings_list_rename_delete(factory):
    app = _app(factory)
    client, soft = await _enrolled_client(app, factory)
    credential_id = bytes_to_base64url(soft.credential_id)

    page = await client.get("/settings/passkeys")
    assert page.status_code == 200
    assert credential_id in page.text

    renamed = await client.post(
        "/settings/passkeys/rename",
        data={"credential_id": credential_id, "name": "yubikey 5c"},
        headers=POST_HEADERS,
    )
    assert renamed.status_code == 303
    assert "yubikey 5c" in (await client.get("/settings/passkeys")).text

    deleted = await client.post(
        "/settings/passkeys/delete",
        data={"credential_id": credential_id},
        headers=POST_HEADERS,
    )
    assert deleted.status_code == 303
    async with factory() as session:
        assert await AuthRepo(session).get_credential(credential_id) is None
    await client.aclose()


async def test_cannot_manage_another_users_credential(factory):
    app = _app(factory)
    victim, soft = await _enrolled_client(app, factory)
    await victim.aclose()
    credential_id = bytes_to_base64url(soft.credential_id)

    # mallory: separate account, own passkey (to get past forced enrollment).
    async with _client(app) as mallory:
        await _sign_in(mallory, factory, "m@example.com")
        assert (await _register(mallory, SoftAuthenticator())).status_code == 200
        await mallory.post(
            "/settings/passkeys/rename",
            data={"credential_id": credential_id, "name": "hijacked"},
            headers=POST_HEADERS,
        )
        await mallory.post(
            "/settings/passkeys/delete",
            data={"credential_id": credential_id},
            headers=POST_HEADERS,
        )

    async with factory() as session:
        row = await AuthRepo(session).get_credential(credential_id)
    assert row is not None  # still exists...
    assert row.name != "hijacked"  # ...and untouched

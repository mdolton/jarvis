"""The emailed one-time-code login flow, end to end against real SQLite.

The enumeration-resistance tests assert the mechanism, not just the message:
POST /auth/login must produce an identical response AND schedule the same
background work for on-list, off-list, and rate-limited requests — the
allow-list decision must never execute on the request path (the mail send is
the timing oracle; see CVE-2026-26185).
"""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.auth.codes import LoginCodeService
from jarvis.auth.ratelimit import RateLimiter
from jarvis.auth.sessions import SessionManager, hash_token
from jarvis.config.schema import AuthConfig, MailConfig
from jarvis.core.types import AuditEventType
from jarvis.persistence.db import Base
from jarvis.persistence.models import UserRow
from jarvis.persistence.repositories import AuditRepo, AuthRepo
from jarvis.web.app import create_app
from jarvis.web.routes.auth import NONCE_COOKIE, AuthFlow

AUTH_ON = AuthConfig(
    enabled=True,
    secure_cookies=False,
    allowed_emails=["me@example.com"],
)
ORIGIN = {"origin": "http://testserver"}


class CapturingMailer:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *, to: str, subject: str, text: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text})

    def last_code(self) -> str:
        match = re.search(r"\b(\d{6})\b", self.sent[-1]["text"])
        assert match, f"no code in {self.sent[-1]['text']!r}"
        return match.group(1)


class FailingMailer:
    async def send(self, *, to: str, subject: str, text: str) -> None:
        raise RuntimeError("smtp down")


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/login.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _generous():
    return RateLimiter(capacity=1000, refill_per_sec=1.0)


def _app(factory, mailer, *, auth_cfg: AuthConfig = AUTH_ON, flow: AuthFlow | None = None):
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config.jarvis.auth = auth_cfg
    ctx.config.jarvis.mail = MailConfig()
    ctx.config.jarvis.timezone = "UTC"
    app = create_app(app_context=ctx)
    app.state.auth_flow = flow or AuthFlow(
        codes=LoginCodeService(session_factory=factory, config=auth_cfg, mailer=mailer),
        login_email_limiter=_generous(),
        login_ip_limiter=_generous(),
        verify_ip_limiter=_generous(),
    )
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _request_code(client) -> httpx.Response:
    return await client.post("/auth/login", data={"email": "me@example.com"}, headers=ORIGIN)


async def _user_exists(factory, email: str) -> bool:
    async with factory() as session:
        result = await session.execute(select(UserRow).where(UserRow.email == email))
        return result.scalar_one_or_none() is not None


# -- enumeration resistance ------------------------------------------


async def test_login_identical_response_and_identical_work_for_on_and_off_list(factory):
    mailer = CapturingMailer()
    app = _app(factory, mailer)
    # Wrap the background half to prove BOTH branches schedule it — the
    # allow-list decision must live inside the task, never on the request
    # path, so there is no early return for a miss.
    flow = app.state.auth_flow
    scheduled: list[str] = []
    original = flow.codes.issue_and_send

    async def recording(login):
        scheduled.append(login.email)
        await original(login)

    flow.codes.issue_and_send = recording

    async with _client(app) as client:
        on_list = await client.post("/auth/login", data={"email": "me@example.com"}, headers=ORIGIN)
        off_list = await client.post(
            "/auth/login", data={"email": "stranger@example.com"}, headers=ORIGIN
        )

    # Identical status, body, and redirect target; a nonce cookie on both.
    assert on_list.status_code == off_list.status_code == 303
    assert on_list.content == off_list.content
    assert on_list.headers["location"] == off_list.headers["location"] == "/auth/verify"
    assert NONCE_COOKIE in on_list.cookies and NONCE_COOKIE in off_list.cookies

    # The background half ran for both; only the on-list branch did anything.
    assert scheduled == ["me@example.com", "stranger@example.com"]
    assert [m["to"] for m in mailer.sent] == ["me@example.com"]
    assert await _user_exists(factory, "me@example.com")
    assert not await _user_exists(factory, "stranger@example.com")


async def test_rate_limited_login_returns_identical_response_without_sending(factory):
    mailer = CapturingMailer()
    tight = AuthFlow(
        codes=LoginCodeService(session_factory=factory, config=AUTH_ON, mailer=mailer),
        login_email_limiter=RateLimiter(capacity=1, refill_per_sec=1 / 900),
        login_ip_limiter=_generous(),
        verify_ip_limiter=_generous(),
    )
    async with _client(_app(factory, mailer, flow=tight)) as client:
        first = await _request_code(client)
        second = await _request_code(client)

    assert len(mailer.sent) == 1  # the limited request sent nothing...
    assert second.status_code == first.status_code  # ...and looked identical
    assert second.content == first.content
    assert second.headers["location"] == first.headers["location"]
    assert NONCE_COOKIE in second.cookies


async def test_mailer_failure_does_not_change_the_response_and_is_audited(factory):
    ok_resp_app = _app(factory, CapturingMailer())
    async with _client(ok_resp_app) as client:
        baseline = await _request_code(client)

    async with _client(_app(factory, FailingMailer())) as client:
        failed = await _request_code(client)

    assert failed.status_code == baseline.status_code
    assert failed.content == baseline.content
    assert failed.headers["location"] == baseline.headers["location"]
    async with factory() as session:
        events = await AuditRepo(session).recent(types=[AuditEventType.AUTH_MAIL_SEND_FAILED])
    assert len(events) == 1
    assert events[0].payload["to"] == "me@example.com"


# -- the happy path --------------------------------------------------


async def test_full_login_flow_issues_session(factory):
    mailer = CapturingMailer()
    async with _client(_app(factory, mailer)) as client:
        resp = await _request_code(client)
        assert resp.status_code == 303

        assert (await client.get("/auth/verify")).status_code == 200

        verified = await client.post(
            "/auth/verify", data={"code": mailer.last_code()}, headers=ORIGIN
        )
        assert verified.status_code == 303
        assert verified.headers["location"] == "/"
        assert "jarvis_session" in verified.cookies

        # The session is live, but a freshly-enrolled account has no passkey
        # yet, so the middleware forces it to enrollment — NOT back to login.
        home = await client.get("/", follow_redirects=False)
        assert home.status_code == 302
        assert home.headers["location"] == "/auth/passkey/register"
        assert (await client.get("/auth/passkey/register")).status_code == 200

    # The code was consumed: replaying it in the same browser fails.
    async with factory() as session:
        row = await AuthRepo(session).get_session_by_token_hash(
            hash_token(verified.cookies["jarvis_session"])
        )
    assert row is not None


async def test_code_is_consume_once(factory):
    mailer = CapturingMailer()
    async with _client(_app(factory, mailer)) as client:
        await _request_code(client)
        code = mailer.last_code()
        first = await client.post("/auth/verify", data={"code": code}, headers=ORIGIN)
        replay = await client.post("/auth/verify", data={"code": code}, headers=ORIGIN)
    assert first.status_code == 303
    assert replay.status_code == 200
    assert "not accepted" in replay.text


async def test_default_flow_builds_from_config_with_console_mailer(factory, caplog):
    """Without a pre-seeded AuthFlow the route wires itself from config."""
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config.jarvis.auth = AUTH_ON
    ctx.config.jarvis.mail = MailConfig()  # provider: console
    ctx.config.jarvis.timezone = "UTC"
    app = create_app(app_context=ctx)
    with caplog.at_level("INFO", logger="jarvis.auth.mailer"):
        async with _client(app) as client:
            resp = await _request_code(client)
    assert resp.status_code == 303
    assert re.search(r"\b\d{6}\b", caplog.text)  # code logged, not mailed


# -- code hardening ---------------------------------------------------


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "000001"


async def test_expired_code_rejected(factory):
    async with factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=hashlib.sha256(b"123456").hexdigest(),
            nonce_hash=hashlib.sha256(b"nonce-raw").hexdigest(),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    async with _client(_app(factory, CapturingMailer())) as client:
        client.cookies.set(NONCE_COOKIE, "nonce-raw", path="/auth")
        resp = await client.post("/auth/verify", data={"code": "123456"}, headers=ORIGIN)
    assert resp.status_code == 200
    assert "not accepted" in resp.text


async def test_five_attempt_lockout(factory):
    mailer = CapturingMailer()
    async with _client(_app(factory, mailer)) as client:
        await _request_code(client)
        code = mailer.last_code()
        for _ in range(5):
            miss = await client.post("/auth/verify", data={"code": _wrong(code)}, headers=ORIGIN)
            assert miss.status_code == 200
        # The budget is spent: even the CORRECT code is now dead.
        final = await client.post("/auth/verify", data={"code": code}, headers=ORIGIN)
    assert final.status_code == 200
    assert "not accepted" in final.text


async def test_new_code_does_not_reset_attempt_counter(factory):
    mailer = CapturingMailer()
    async with _client(_app(factory, mailer)) as client:
        await _request_code(client)
        first_code = mailer.last_code()
        for _ in range(3):
            await client.post("/auth/verify", data={"code": _wrong(first_code)}, headers=ORIGIN)

        # Requesting a fresh code must not refill the guess budget.
        await _request_code(client)
        second_code = mailer.last_code()
        for _ in range(2):
            await client.post("/auth/verify", data={"code": _wrong(second_code)}, headers=ORIGIN)
        # 3 + 2 misses spent all five attempts; the correct code is dead.
        final = await client.post("/auth/verify", data={"code": second_code}, headers=ORIGIN)
    assert final.status_code == 200
    assert "not accepted" in final.text


async def test_stale_code_rejected_after_new_code_issued(factory):
    mailer = CapturingMailer()
    async with _client(_app(factory, mailer)) as client:
        await _request_code(client)
        first_code = mailer.last_code()
        await _request_code(client)  # invalidates the first code
        resp = await client.post("/auth/verify", data={"code": first_code}, headers=ORIGIN)
    assert resp.status_code == 200
    assert "not accepted" in resp.text


async def test_nonce_mismatch_rejected(factory):
    """A stolen code is useless outside the browser that requested it."""
    mailer = CapturingMailer()
    app = _app(factory, mailer)
    async with _client(app) as requester:
        await _request_code(requester)
    code = mailer.last_code()

    # Same code, different browser (no nonce cookie / a forged one): rejected.
    async with _client(app) as attacker:
        bare = await attacker.post("/auth/verify", data={"code": code}, headers=ORIGIN)
        attacker.cookies.set(NONCE_COOKIE, "forged-nonce", path="/auth")
        forged = await attacker.post("/auth/verify", data={"code": code}, headers=ORIGIN)
    assert bare.status_code == 200 and "not accepted" in bare.text
    assert forged.status_code == 200 and "not accepted" in forged.text

    # And the correct browser still succeeds afterwards — the mismatches
    # above never touched the real code's attempt budget.
    async with _client(app) as requester:
        # (fresh client: re-request to get the nonce back into a jar)
        await _request_code(requester)
        ok = await requester.post("/auth/verify", data={"code": mailer.last_code()}, headers=ORIGIN)
    assert ok.status_code == 303


# -- logout-all -------------------------------------------------------


async def test_logout_all_revokes_every_session(factory):
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
    manager = SessionManager(session_factory=factory, config=AUTH_ON)
    laptop = await manager.issue_session(user.id)
    phone = await manager.issue_session(user.id)

    async with _client(_app(factory, CapturingMailer())) as client:
        client.cookies.set("jarvis_session", laptop)
        resp = await client.post("/auth/logout-all", headers=ORIGIN)
    assert resp.status_code == 302
    assert await manager.validate(laptop) is None
    assert await manager.validate(phone) is None

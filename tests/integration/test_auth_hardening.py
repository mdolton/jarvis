"""Login-page hardening + the auth audit trail.

- Exponential per-IP backoff on repeated login failures (code and passkey
  paths share one lockout), always behind the SAME generic failure response.
- Global cap on in-flight (unconsumed, unexpired) login codes, enforced off
  the request path.
- Audit events for logins, code requests, ceremonies, logout, revocations
  and rate-limit trips — each carrying the client IP and user agent.
"""

import re
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.auth.codes import LoginCodeService
from jarvis.auth.ratelimit import ExponentialBackoff, RateLimiter
from jarvis.auth.sessions import SessionManager
from jarvis.config.schema import AuthConfig, MailConfig
from jarvis.core.types import AuditEventType
from jarvis.persistence.db import Base
from jarvis.persistence.repositories import AuditRepo, AuthRepo
from jarvis.web.app import create_app
from jarvis.web.csrf import csrf_token_for_session
from jarvis.web.routes.auth import AuthFlow
from tests.integration.test_passkeys import SoftAuthenticator

AUTH_ON = AuthConfig(enabled=True, secure_cookies=False, allowed_emails=["me@example.com"])
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/hardening.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _generous():
    return RateLimiter(capacity=1000, refill_per_sec=1.0)


def _flow(factory, mailer, *, auth_cfg=AUTH_ON, clock=None, **flow_overrides) -> AuthFlow:
    kwargs = dict(
        codes=LoginCodeService(session_factory=factory, config=auth_cfg, mailer=mailer),
        login_email_limiter=_generous(),
        login_ip_limiter=_generous(),
        verify_ip_limiter=_generous(),
    )
    if clock is not None:
        kwargs["login_backoff"] = ExponentialBackoff(clock=clock)
    kwargs.update(flow_overrides)
    return AuthFlow(**kwargs)


def _app(factory, flow: AuthFlow, *, auth_cfg=AUTH_ON):
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config.jarvis.auth = auth_cfg
    ctx.config.jarvis.mail = MailConfig()
    ctx.config.jarvis.timezone = "UTC"
    app = create_app(app_context=ctx)
    app.state.auth_flow = flow
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _events(factory, type_: AuditEventType):
    async with factory() as session:
        return await AuditRepo(session).recent(types=[type_])


async def _request_code(client, email="me@example.com"):
    return await client.post("/auth/login", data={"email": email}, headers=ORIGIN)


async def _verify(client, code: str):
    return await client.post("/auth/verify", data={"code": code}, headers=ORIGIN)


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "000001"


# -- exponential backoff ----------------------------------------------


async def test_backoff_locks_out_repeated_failures_and_expires(factory):
    clock = FakeClock()
    mailer = CapturingMailer()
    async with _client(_app(factory, _flow(factory, mailer, clock=clock))) as client:
        await _request_code(client)
        code = mailer.last_code()

        # 3 free misses, the 4th earns a 1s lockout.
        for _ in range(4):
            resp = await _verify(client, _wrong(code))
            assert resp.status_code == 200 and "not accepted" in resp.text

        # Locked out: even the CORRECT code gets the same generic failure,
        # and the attempt budget is NOT spent (the verify never runs).
        blocked = await _verify(client, code)
        assert blocked.status_code == 200
        assert "not accepted" in blocked.text
        trips = await _events(factory, AuditEventType.AUTH_RATE_LIMITED)
        assert [e.payload["scope"] for e in trips] == ["login_backoff"]

        # After the lockout expires the correct code still works — proof the
        # blocked attempt above didn't burn one of the 5 attempts (4 misses
        # + 1 real attempt = exactly the budget).
        clock.now += 1.1
        ok = await _verify(client, code)
        assert ok.status_code == 303


async def test_backoff_delay_doubles_per_failure(factory):
    clock = FakeClock()
    backoff = ExponentialBackoff(clock=clock)  # 3 free, then 1s, 2s, 4s...
    for _ in range(4):
        backoff.record_failure("ip")
    assert not backoff.allowed("ip")
    clock.now += 1.01  # first lockout: 1s
    assert backoff.allowed("ip")

    backoff.record_failure("ip")
    clock.now += 1.01
    assert not backoff.allowed("ip")  # second lockout: 2s
    clock.now += 1.01
    assert backoff.allowed("ip")

    backoff.reset("ip")
    backoff.record_failure("ip")
    assert backoff.allowed("ip")  # reset restored the free misses


async def test_backoff_also_covers_passkey_login_failures(factory):
    clock = FakeClock()
    flow = _flow(factory, CapturingMailer(), clock=clock)
    app = _app(factory, flow)
    async with _client(app) as client:
        for _ in range(4):
            begin = (await client.post("/auth/passkey/login/begin", headers=ORIGIN)).json()
            resp = await client.post(
                "/auth/passkey/login/complete",
                json={
                    "challenge_id": begin["challenge_id"],
                    "credential": SoftAuthenticator().get(begin["options"]),
                },
                headers=ORIGIN,
            )
            assert resp.status_code == 401  # unknown credential

        # Locked out: the next attempt is rejected before any ceremony runs.
        begin = (await client.post("/auth/passkey/login/begin", headers=ORIGIN)).json()
        blocked = await client.post(
            "/auth/passkey/login/complete",
            json={
                "challenge_id": begin["challenge_id"],
                "credential": SoftAuthenticator().get(begin["options"]),
            },
            headers=ORIGIN,
        )
    assert blocked.status_code == 401
    trips = await _events(factory, AuditEventType.AUTH_RATE_LIMITED)
    assert "login_backoff" in [e.payload["scope"] for e in trips]


# -- global in-flight code cap ----------------------------------------


async def test_global_inflight_code_cap_stops_issuing(factory):
    cfg = AuthConfig(
        enabled=True,
        secure_cookies=False,
        allowed_emails=["a@example.com", "b@example.com"],
        max_inflight_codes=1,
    )
    mailer = CapturingMailer()
    async with _client(_app(factory, _flow(factory, mailer, auth_cfg=cfg), auth_cfg=cfg)) as c:
        first = await _request_code(c, "a@example.com")
        second = await _request_code(c, "b@example.com")

    # Identical responses either way — the cap is enforced off the request
    # path — but only the first code was actually issued and mailed.
    assert first.status_code == second.status_code == 303
    assert [m["to"] for m in mailer.sent] == ["a@example.com"]
    trips = await _events(factory, AuditEventType.AUTH_RATE_LIMITED)
    assert [e.payload["scope"] for e in trips] == ["global_inflight_codes"]
    assert trips[0].payload["email"] == "b@example.com"


async def test_reissuing_to_the_same_user_replaces_within_the_cap(factory):
    cfg = AuthConfig(
        enabled=True, secure_cookies=False, allowed_emails=["me@example.com"], max_inflight_codes=2
    )
    mailer = CapturingMailer()
    async with _client(_app(factory, _flow(factory, mailer, auth_cfg=cfg), auth_cfg=cfg)) as c:
        await _request_code(c)
        await _request_code(c)  # replaces, does not stack
        await _request_code(c)
    assert len(mailer.sent) == 3  # replace_auth_code keeps the pool at 1


# -- audit trail --------------------------------------------------------


async def test_code_request_and_login_attempts_are_audited_with_ip_and_ua(factory):
    mailer = CapturingMailer()
    async with _client(_app(factory, _flow(factory, mailer))) as client:
        await _request_code(client)
        code = mailer.last_code()
        await _verify(client, _wrong(code))
        await _verify(client, code)

    requested = await _events(factory, AuditEventType.AUTH_LOGIN_CODE_REQUESTED)
    assert len(requested) == 1
    assert requested[0].payload["email"] == "me@example.com"
    assert requested[0].payload["ip"] == "127.0.0.1"
    assert "httpx" in requested[0].payload["user_agent"]
    assert requested[0].payload["rate_limited"] is False

    failed = await _events(factory, AuditEventType.AUTH_LOGIN_FAILED)
    assert len(failed) == 1
    assert failed[0].payload["method"] == "code"
    assert failed[0].payload["ip"] == "127.0.0.1"

    succeeded = await _events(factory, AuditEventType.AUTH_LOGIN_SUCCEEDED)
    assert len(succeeded) == 1
    assert succeeded[0].payload["method"] == "code"
    assert succeeded[0].payload["user_agent"]


async def test_rate_limit_trips_on_code_requests_are_audited(factory):
    mailer = CapturingMailer()
    flow = _flow(
        factory, mailer, login_email_limiter=RateLimiter(capacity=1, refill_per_sec=1 / 900)
    )
    async with _client(_app(factory, flow)) as client:
        await _request_code(client)
        await _request_code(client)  # trips the per-address bucket

    trips = await _events(factory, AuditEventType.AUTH_RATE_LIMITED)
    assert len(trips) == 1
    assert trips[0].payload["scope"] == ["login_email"]
    assert trips[0].payload["ip"] == "127.0.0.1"
    requested = await _events(factory, AuditEventType.AUTH_LOGIN_CODE_REQUESTED)
    assert [e.payload["rate_limited"] for e in requested] == [True, False]  # newest first


async def test_verify_rate_limit_trip_is_audited(factory):
    flow = _flow(
        factory,
        CapturingMailer(),
        verify_ip_limiter=RateLimiter(capacity=1, refill_per_sec=1 / 900),
    )
    async with _client(_app(factory, flow)) as client:
        await _verify(client, "000000")  # spends the only token (fails: no code)
        await _verify(client, "000000")  # tripped
    trips = await _events(factory, AuditEventType.AUTH_RATE_LIMITED)
    assert [e.payload["scope"] for e in trips] == ["verify_ip"]


async def test_logout_and_logout_all_are_audited(factory):
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
    manager = SessionManager(session_factory=factory, config=AUTH_ON)
    app = _app(factory, _flow(factory, CapturingMailer()))

    raw = await manager.issue_session(user.id)
    async with _client(app) as client:
        client.cookies.set("jarvis_session", raw)
        client.headers["X-CSRF-Token"] = csrf_token_for_session(raw)
        await client.post("/auth/logout", headers=ORIGIN)
    logouts = await _events(factory, AuditEventType.AUTH_LOGOUT)
    assert len(logouts) == 1
    assert logouts[0].payload["ip"] == "127.0.0.1"

    raw = await manager.issue_session(user.id)  # fresh: last_auth_at = now
    async with _client(app) as client:
        client.cookies.set("jarvis_session", raw)
        client.headers["X-CSRF-Token"] = csrf_token_for_session(raw)
        await client.post("/auth/logout-all", headers=ORIGIN)
    revoked = await _events(factory, AuditEventType.AUTH_SESSIONS_REVOKED)
    assert len(revoked) == 1
    assert revoked[0].payload["email"] == "me@example.com"
    assert revoked[0].payload["user_agent"]


async def test_passkey_ceremonies_are_audited(factory):
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
    raw = await SessionManager(session_factory=factory, config=AUTH_ON).issue_session(user.id)
    app = _app(factory, _flow(factory, CapturingMailer()))
    soft = SoftAuthenticator()

    async with _client(app) as client:
        client.cookies.set("jarvis_session", raw)
        client.headers["X-CSRF-Token"] = csrf_token_for_session(raw)
        begin = (await client.post("/auth/passkey/register/begin", headers=ORIGIN)).json()
        resp = await client.post(
            "/auth/passkey/register/complete",
            json={
                "challenge_id": begin["challenge_id"],
                "credential": soft.create(begin["options"]),
                "name": "test key",
            },
            headers=ORIGIN,
        )
        assert resp.status_code == 200, resp.text

    registered = await _events(factory, AuditEventType.AUTH_PASSKEY_REGISTERED)
    assert len(registered) == 1
    assert registered[0].payload["email"] == "me@example.com"
    assert registered[0].payload["name"] == "test key"
    assert registered[0].payload["ip"] == "127.0.0.1"

    # Passkey login (fresh, anonymous client) → login_succeeded via passkey.
    async with _client(app) as client:
        begin = (await client.post("/auth/passkey/login/begin", headers=ORIGIN)).json()
        resp = await client.post(
            "/auth/passkey/login/complete",
            json={"challenge_id": begin["challenge_id"], "credential": soft.get(begin["options"])},
            headers=ORIGIN,
        )
        assert resp.status_code == 200, resp.text
    succeeded = await _events(factory, AuditEventType.AUTH_LOGIN_SUCCEEDED)
    assert [e.payload["method"] for e in succeeded] == ["passkey"]
    assert succeeded[0].payload["email"] == "me@example.com"

"""CSRF synchronizer token: every unsafe-method request that carries a
session cookie must present the matching per-session token (header or form
field), on top of the same-origin check. Requests without a session cookie
are the pre-auth login routes — covered by origin + the login nonce.
"""

from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.auth.sessions import SessionManager
from jarvis.config.schema import AuthConfig, MailConfig
from jarvis.persistence.db import Base
from jarvis.persistence.repositories import AuthRepo
from jarvis.web.app import create_app
from jarvis.web.csrf import csrf_token_for_session

AUTH_ON = AuthConfig(enabled=True, secure_cookies=False, allowed_emails=["me@example.com"])
ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/csrf.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _app(factory):
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config.jarvis.auth = AUTH_ON
    ctx.config.jarvis.mail = MailConfig()
    ctx.config.jarvis.timezone = "UTC"
    return create_app(app_context=ctx)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _login(factory) -> str:
    """Create a user with a live session; returns the raw session token."""
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
    manager = SessionManager(session_factory=factory, config=AUTH_ON)
    return await manager.issue_session(user.id)


async def test_post_with_session_but_no_token_is_rejected(factory):
    raw = await _login(factory)
    async with _client(_app(factory)) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.post("/auth/logout", headers=ORIGIN)
    assert resp.status_code == 403
    assert "CSRF" in resp.text


async def test_post_with_wrong_token_is_rejected(factory):
    raw = await _login(factory)
    async with _client(_app(factory)) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.post("/auth/logout", headers={**ORIGIN, "X-CSRF-Token": "f" * 64})
    assert resp.status_code == 403


async def test_header_token_accepted(factory):
    raw = await _login(factory)
    token = csrf_token_for_session(raw)
    async with _client(_app(factory)) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.post("/auth/logout", headers={**ORIGIN, "X-CSRF-Token": token})
    assert resp.status_code == 302  # reached the route


async def test_form_field_token_accepted_and_body_still_reaches_handler(factory):
    # The middleware parses the form to find csrf_token; the handler must
    # still see the OTHER fields. Asserting on a response that looks the
    # same either way would be worthless — instead prove the `email` field
    # reached the handler: the captured mail can only carry the address if
    # the body survived the middleware's read. (Regression: form() without
    # body() consumes the stream uncached and downstream sees an empty form
    # — caught in a real browser by the step-up replay page echoing zero
    # fields.)
    raw = await _login(factory)
    token = csrf_token_for_session(raw)
    app = _app(factory)

    from jarvis.auth.codes import LoginCodeService
    from jarvis.auth.ratelimit import RateLimiter
    from jarvis.web.routes.auth import AuthFlow

    class CapturingMailer:
        def __init__(self):
            self.sent = []

        async def send(self, *, to, subject, text):
            self.sent.append(to)

    mailer = CapturingMailer()
    app.state.auth_flow = AuthFlow(
        codes=LoginCodeService(session_factory=factory, config=AUTH_ON, mailer=mailer),
        login_email_limiter=RateLimiter(capacity=100, refill_per_sec=1),
        login_ip_limiter=RateLimiter(capacity=100, refill_per_sec=1),
        verify_ip_limiter=RateLimiter(capacity=100, refill_per_sec=1),
    )
    async with _client(app) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.post(
            "/auth/login",
            data={"email": "me@example.com", "csrf_token": token},
            headers=ORIGIN,
        )
    assert resp.status_code == 303
    assert mailer.sent == ["me@example.com"]  # the form survived the middleware


async def test_request_without_session_cookie_is_not_csrf_gated(factory):
    # Pre-auth: the login POST has no session yet; origin check + login nonce
    # cover it, and demanding a token would break the login page.
    async with _client(_app(factory)) as client:
        resp = await client.post("/auth/login", data={"email": "x@example.com"}, headers=ORIGIN)
    assert resp.status_code == 303


async def test_missing_secrets_key_fails_closed(factory, monkeypatch):
    raw = await _login(factory)
    token = csrf_token_for_session(raw)
    monkeypatch.delenv("JARVIS_SECRETS_KEY", raising=False)
    async with _client(_app(factory)) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.post("/auth/logout", headers={**ORIGIN, "X-CSRF-Token": token})
    assert resp.status_code == 403
    assert "JARVIS_SECRETS_KEY" in resp.text


async def test_pages_embed_the_token_for_forms_htmx_and_fetch(factory):
    # One render carries the token three ways: hidden input (plain forms),
    # body hx-headers (htmx inheritance), meta tag (passkeys.js fetch).
    raw = await _login(factory)
    token = csrf_token_for_session(raw)
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
        await AuthRepo(session).add_credential(
            credential_id="cred-1", user_id=user.id, public_key=b"pk"
        )
    async with _client(_app(factory)) as client:
        client.cookies.set("jarvis_session", raw)
        page = await client.get("/settings/passkeys")
    assert page.status_code == 200
    assert f'<input type="hidden" name="csrf_token" value="{token}">' in page.text
    assert f'hx-headers=\'{{"X-CSRF-Token": "{token}"}}\'' in page.text
    assert f'<meta name="csrf-token" content="{token}">' in page.text


async def test_login_page_renders_without_a_session(factory):
    async with _client(_app(factory)) as client:
        page = await client.get("/auth/login")
    assert page.status_code == 200
    assert 'name="csrf_token" value=""' in page.text  # empty pre-session

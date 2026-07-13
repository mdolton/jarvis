"""Step-up re-authentication: sensitive routes demand a passkey assertion
fresher than auth.step_up_window_minutes (sessions.last_auth_at).

Uses the SoftAuthenticator from test_passkeys — real py_webauthn verification,
no stubs. POST /settings/model stands in for the gated mutations (it only
needs mocked model_store/audit); /auth/logout-all covers the middleware-exempt
path where the dependency must validate the cookie itself.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.auth.sessions import SessionManager
from jarvis.config.schema import AuthConfig
from jarvis.core.types import AuditEventType
from jarvis.persistence.db import Base
from jarvis.persistence.models import SessionRow
from jarvis.persistence.repositories import AuthRepo
from jarvis.web.app import create_app
from tests.integration.test_passkeys import SoftAuthenticator

AUTH_ON = AuthConfig(enabled=True, secure_cookies=False, allowed_emails=["me@example.com"])
POST_HEADERS = {"origin": "http://testserver"}  # same-origin middleware
HTMX_HEADERS = {**POST_HEADERS, "HX-Request": "true"}


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/stepup.db")
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
    ctx.model_store.current.return_value = "cfg-model"
    ctx.model_store.set = AsyncMock()
    ctx.audit = MagicMock()
    ctx.audit.emit = AsyncMock()
    return create_app(app_context=ctx), ctx


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _enrolled_client(app, factory) -> tuple[httpx.AsyncClient, SoftAuthenticator]:
    """Signed-in client with a registered passkey (register stamps nothing;
    issue_session sets last_auth_at=now, so the session starts FRESH)."""
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
    raw = await SessionManager(session_factory=factory, config=AUTH_ON).issue_session(user.id)
    client = _client(app)
    client.cookies.set("jarvis_session", raw)
    begin = (await client.post("/auth/passkey/register/begin", headers=POST_HEADERS)).json()
    soft = SoftAuthenticator()
    resp = await client.post(
        "/auth/passkey/register/complete",
        json={"challenge_id": begin["challenge_id"], "credential": soft.create(begin["options"])},
        headers=POST_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return client, soft


async def _make_stale(factory) -> None:
    async with factory() as session:
        await session.execute(
            update(SessionRow).values(last_auth_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        await session.commit()


async def _last_auth_at(factory) -> datetime:
    async with factory() as session:
        row = (await session.execute(select(SessionRow))).scalar_one()
    return row.last_auth_at


def _audited(ctx) -> list[AuditEventType]:
    return [call.args[0].type for call in ctx.audit.emit.await_args_list]


async def _step_up(client, soft: SoftAuthenticator) -> httpx.Response:
    begin = (await client.post("/auth/step-up/begin", headers=POST_HEADERS)).json()
    return await client.post(
        "/auth/step-up/complete",
        json={"challenge_id": begin["challenge_id"], "credential": soft.get(begin["options"])},
        headers=POST_HEADERS,
    )


async def test_fresh_session_passes(factory):
    app, ctx = _app(factory)
    client, _ = await _enrolled_client(app, factory)
    resp = await client.post("/settings/model", data={"model": "alpha"}, headers=POST_HEADERS)
    assert resp.status_code == 303
    ctx.model_store.set.assert_awaited_once_with("alpha")
    assert AuditEventType.AUTH_STEP_UP_CHALLENGED not in _audited(ctx)


async def test_stale_session_challenged_with_replay_form(factory):
    app, ctx = _app(factory)
    client, _ = await _enrolled_client(app, factory)
    await _make_stale(factory)
    resp = await client.post("/settings/model", data={"model": "alpha"}, headers=POST_HEADERS)
    assert resp.status_code == 401
    # The submitted field is echoed into the hidden replay form — nothing the
    # user typed is lost across the challenge.
    assert 'id="step-up-replay"' in resp.text
    assert 'action="/settings/model"' in resp.text
    assert 'name="model" value="alpha"' in resp.text
    ctx.model_store.set.assert_not_awaited()
    assert AuditEventType.AUTH_STEP_UP_CHALLENGED in _audited(ctx)


async def test_stale_session_htmx_gets_401_hx_trigger(factory):
    app, ctx = _app(factory)
    client, _ = await _enrolled_client(app, factory)
    await _make_stale(factory)
    resp = await client.post("/settings/model", data={"model": "alpha"}, headers=HTMX_HEADERS)
    assert resp.status_code == 401
    # No redirect: the challenge is an HX-Trigger event that opens the modal.
    assert "hx-redirect" not in {k.lower() for k in resp.headers}
    assert "jarvis-step-up-required" in resp.headers["HX-Trigger"]
    ctx.model_store.set.assert_not_awaited()


async def test_stale_get_route_challenged_as_page(factory):
    app, ctx = _app(factory)
    client, _ = await _enrolled_client(app, factory)
    await _make_stale(factory)
    resp = await client.get("/oauth/connect/00000000000000000000000000000000")
    assert resp.status_code == 401
    assert 'data-method="GET"' in resp.text
    assert AuditEventType.AUTH_STEP_UP_CHALLENGED in _audited(ctx)


async def test_successful_assertion_updates_last_auth_at_and_unblocks(factory):
    app, ctx = _app(factory)
    client, soft = await _enrolled_client(app, factory)
    await _make_stale(factory)
    before = await _last_auth_at(factory)

    resp = await _step_up(client, soft)
    assert resp.status_code == 200
    assert resp.json() == {"verified": True}
    assert await _last_auth_at(factory) > before
    assert AuditEventType.AUTH_STEP_UP_SUCCEEDED in _audited(ctx)

    resp = await client.post("/settings/model", data={"model": "alpha"}, headers=POST_HEADERS)
    assert resp.status_code == 303
    ctx.model_store.set.assert_awaited_once_with("alpha")


async def test_failed_assertion_audited_and_still_stale(factory):
    app, ctx = _app(factory)
    client, _ = await _enrolled_client(app, factory)
    await _make_stale(factory)
    before = await _last_auth_at(factory)

    # A different authenticator: unknown credential, generic 401.
    resp = await _step_up(client, SoftAuthenticator())
    assert resp.status_code == 401
    assert resp.json()["verified"] is False
    assert await _last_auth_at(factory) == before
    assert AuditEventType.AUTH_STEP_UP_FAILED in _audited(ctx)
    assert AuditEventType.AUTH_STEP_UP_SUCCEEDED not in _audited(ctx)

    resp = await client.post("/settings/model", data={"model": "alpha"}, headers=POST_HEADERS)
    assert resp.status_code == 401


async def test_unauthenticated_never_reaches_step_up(factory):
    app, ctx = _app(factory)
    async with _client(app) as client:
        # Middleware-gated route: the session middleware answers first.
        resp = await client.post("/settings/model", data={"model": "alpha"}, headers=POST_HEADERS)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"
        # Middleware-exempt route (/auth/*): the dependency itself must give
        # the login answer, not a step-up challenge.
        resp = await client.post("/auth/logout-all", headers=POST_HEADERS)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/auth/login"
        # And the ceremony endpoints reject anonymous callers outright.
        resp = await client.post("/auth/step-up/begin", headers=POST_HEADERS)
        assert resp.status_code == 401
    assert not any(t.value.startswith("auth.step_up") for t in _audited(ctx))


async def test_logout_all_requires_fresh_assertion(factory):
    app, _ctx = _app(factory)
    client, _ = await _enrolled_client(app, factory)
    await _make_stale(factory)
    resp = await client.post("/auth/logout-all", headers=POST_HEADERS)
    assert resp.status_code == 401
    # Session still live: the stale challenge must not have revoked anything.
    async with factory() as session:
        row = (await session.execute(select(SessionRow))).scalar_one()
    assert row.revoked_at is None

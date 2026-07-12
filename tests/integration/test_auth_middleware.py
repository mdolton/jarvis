import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import jarvis.web.routes.events as events_module
from jarvis.auth.sessions import SessionManager, hash_token
from jarvis.config.schema import AuthConfig
from jarvis.persistence.db import Base
from jarvis.persistence.repositories import AuthRepo
from jarvis.web.app import create_app

# secure_cookies=False → plain "jarvis_session" cookie; tests speak http.
AUTH_ON = AuthConfig(enabled=True, secure_cookies=False)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/auth.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _app(factory, auth_cfg: AuthConfig):
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config.jarvis.auth = auth_cfg
    ctx.config.jarvis.timezone = "UTC"
    ctx.config.jarvis.events.webhook_token = None
    app = create_app(app_context=ctx)

    @app.get("/whoami")
    async def whoami(request: Request):
        user = request.state.user
        return {"email": user.email if user is not None else None}

    return app


def _client(app) -> httpx.AsyncClient:
    # Single-loop ASGI client: TestClient's portal thread would fight the
    # fixture's event loop over aiosqlite connections.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _login(factory, auth_cfg: AuthConfig) -> str:
    """Create a user + session directly; returns the raw token."""
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
    manager = SessionManager(session_factory=factory, config=auth_cfg)
    return await manager.issue_session(user.id)


async def test_exempt_paths_reachable_without_session(factory):
    async with _client(_app(factory, AUTH_ON)) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/auth/login")).status_code == 200
        assert (await client.get("/static/style.css")).status_code == 200
        # Bearer-authed machine endpoint: session auth must not shadow its
        # 404-when-disabled behavior (no webhook_token configured here).
        assert (await client.post("/events/webhook", content=b"{}")).status_code == 404


async def test_protected_path_redirects_html_to_login(factory):
    async with _client(_app(factory, AUTH_ON)) as client:
        resp = await client.get("/whoami")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/login"


async def test_protected_path_htmx_gets_401_hx_redirect(factory):
    async with _client(_app(factory, AUTH_ON)) as client:
        resp = await client.get("/whoami", headers={"HX-Request": "true"})
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/auth/login"


async def test_valid_session_passes_and_sets_request_state_user(factory):
    raw = await _login(factory, AUTH_ON)
    async with _client(_app(factory, AUTH_ON)) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"email": "me@example.com"}


async def test_expired_session_rejected(factory):
    async with factory() as session:
        user = await AuthRepo(session).get_or_create_user("me@example.com")
        await AuthRepo(session).create_session(
            user_id=user.id,
            token_hash=hash_token("expired-raw"),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    async with _client(_app(factory, AUTH_ON)) as client:
        client.cookies.set("jarvis_session", "expired-raw")
        resp = await client.get("/whoami")
    assert resp.status_code == 302


async def test_revoked_session_rejected(factory):
    raw = await _login(factory, AUTH_ON)
    await SessionManager(session_factory=factory, config=AUTH_ON).revoke(raw)
    async with _client(_app(factory, AUTH_ON)) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.get("/whoami")
    assert resp.status_code == 302


async def test_auth_disabled_passes_everything_through(factory):
    async with _client(_app(factory, AuthConfig(enabled=False))) as client:
        resp = await client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"email": None}


async def test_logout_revokes_session_and_clears_cookie(factory):
    raw = await _login(factory, AUTH_ON)
    async with _client(_app(factory, AUTH_ON)) as client:
        client.cookies.set("jarvis_session", raw)
        resp = await client.post("/auth/logout", headers={"origin": "http://testserver"})
    assert resp.status_code == 302
    assert 'jarvis_session=""' in resp.headers.get("set-cookie", "")
    assert await SessionManager(session_factory=factory, config=AUTH_ON).validate(raw) is None


async def test_sse_stream_terminates_after_revocation(factory, monkeypatch):
    # Re-check the session every loop (~1s) instead of every 10 so the test is
    # fast. The generator is driven directly: httpx's ASGITransport buffers
    # the whole response, so an endless SSE body can't be consumed through it.
    monkeypatch.setattr(events_module, "SESSION_RECHECK_LOOPS", 1)
    raw = await _login(factory, AUTH_ON)
    manager = SessionManager(session_factory=factory, config=AUTH_ON)

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.config.jarvis.auth = AUTH_ON
    ctx.config.jarvis.timezone = "UTC"
    app = MagicMock()
    app.state.ctx = ctx

    async def receive():
        await asyncio.sleep(3600)  # client never disconnects on its own
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/events/stream",
            "headers": [(b"cookie", f"jarvis_session={raw}".encode())],
            "query_string": b"",
            "app": app,
        },
        receive,
    )
    response = await events_module.events_stream(request)
    stream = response.body_iterator

    assert ": connected" in await stream.__anext__()
    await manager.revoke(raw)

    async def _drain():
        async for _ in stream:
            pass

    # The generator must notice the revocation and end the stream — before
    # this change it would tick forever.
    await asyncio.wait_for(_drain(), timeout=10)

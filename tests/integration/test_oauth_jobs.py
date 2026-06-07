"""APScheduler job functions for OAuth refresh + pending sweep."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.scheduler.oauth_jobs import oauth_pending_sweep, oauth_token_refresh


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


def fastmail_metadata():
    return {
        "issuer": "https://api.fastmail.com",
        "authorization_endpoint": "https://api.fastmail.com/oauth/authorize",
        "token_endpoint": "https://api.fastmail.com/oauth/token",
        "registration_endpoint": "https://api.fastmail.com/oauth/register",
        "revocation_endpoint": None,
        "code_challenge_methods_supported": ["S256"],
    }


class _MgrStub:
    def __init__(self):
        self.replaced = []
        self.removed = []

    async def replace_oauth_server(self, key, *, url, headers):
        self.replaced.append((key, headers))

    async def remove_oauth_server(self, key):
        self.removed.append(key)


class _MgrReplaceHangs(_MgrStub):
    async def replace_oauth_server(self, key, *, url, headers):
        self.replaced.append((key, headers))
        await asyncio.Event().wait()


async def test_refresh_job_refreshes_due_provider_and_swaps_server(factory):
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="fastmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=encrypt_blob(b"sec", key),
            access_token_enc=encrypt_blob(b"AT-old", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(seconds=30),  # within 90s window
            scopes_granted=[],
        )

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "AT-NEW", "refresh_token": "RT2", "expires_in": 3600},
            )
        return httpx.Response(404)

    flow = OAuthFlow(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_factory=factory,
        base_url="http://localhost:8080",
        secrets_key=key,
    )
    mgr = _MgrStub()

    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)
    assert mgr.replaced == [("fastmail", {"Authorization": "Bearer AT-NEW"})]


async def test_refresh_job_times_out_hung_server_swap(factory, monkeypatch):
    from jarvis.scheduler import oauth_jobs

    monkeypatch.setattr(oauth_jobs, "OAUTH_REFRESH_ATTACH_TIMEOUT", 0.01)
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="fastmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=encrypt_blob(b"sec", key),
            access_token_enc=encrypt_blob(b"AT-old", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(seconds=30),
            scopes_granted=[],
        )

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "AT-NEW", "refresh_token": "RT2", "expires_in": 3600},
            )
        return httpx.Response(404)

    flow = OAuthFlow(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_factory=factory,
        base_url="http://localhost:8080",
        secrets_key=key,
    )
    mgr = _MgrReplaceHangs()

    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)
    assert mgr.replaced == [("fastmail", {"Authorization": "Bearer AT-NEW"})]


async def test_refresh_job_marks_needs_reauth_on_invalid_grant(factory):
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="fastmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=None,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now,  # due now
            scopes_granted=[],
        )

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        return httpx.Response(400, json={"error": "invalid_grant"})

    flow = OAuthFlow(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_factory=factory,
        base_url="http://localhost:8080",
        secrets_key=key,
    )
    mgr = _MgrStub()
    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)
    assert mgr.removed == ["fastmail"]
    async with factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert cred.status == "needs_reauth"


async def test_pending_sweep_removes_old_rows(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthPendingRepo(session)
        await repo.insert(
            state="old",
            provider_key="fastmail",
            code_verifier="v",
            now=now - timedelta(hours=2),
        )
        await repo.insert(
            state="new",
            provider_key="fastmail",
            code_verifier="v",
            now=now - timedelta(seconds=10),
        )
    n = await oauth_pending_sweep(session_factory=factory, ttl_seconds=600)
    assert n == 1
    async with factory() as session:
        assert await OAuthPendingRepo(session).get("old") is None
        assert await OAuthPendingRepo(session).get("new") is not None

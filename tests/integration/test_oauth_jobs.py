"""APScheduler job functions for OAuth refresh + pending sweep."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio

from jarvis.oauth.catalog import seed_built_in_providers
from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.flow import OAuthRefreshPermanentError, OAuthRefreshTransientError
from jarvis.oauth.store import MCPConnectionRepo, MCPPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.scheduler.oauth_jobs import oauth_pending_sweep, oauth_token_refresh


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_refresh_due_connection_updates_token_in_place(factory):
    key = generate_key().encode()
    async with factory() as s:
        c = await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                              runtime_name="calendar:w")
        await MCPConnectionRepo(s).set_tokens(
            c.id, access_token_enc=encrypt_blob(b"old", key), refresh_token_enc=encrypt_blob(b"r", key),
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30), scopes_granted=[])

    flow = MagicMock()
    flow.refresh = AsyncMock(return_value={"Authorization": "Bearer NEW"})
    mgr = MagicMock()
    mgr.update_oauth_token = MagicMock(return_value=True)  # live holder present

    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)

    flow.refresh.assert_awaited_once_with(c.id)
    mgr.update_oauth_token.assert_called_once_with("calendar:w", "NEW")


async def test_refresh_job_transient_error_is_skipped(factory):
    key = generate_key().encode()
    async with factory() as s:
        c = await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                              runtime_name="calendar:w")
        await MCPConnectionRepo(s).set_tokens(
            c.id, access_token_enc=encrypt_blob(b"old", key), refresh_token_enc=encrypt_blob(b"r", key),
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30), scopes_granted=[])

    flow = MagicMock()
    flow.refresh = AsyncMock(side_effect=OAuthRefreshTransientError("network hiccup"))
    mgr = MagicMock()
    mgr.update_oauth_token = MagicMock()
    mgr.remove_oauth_server = AsyncMock()

    # Should not raise; transient errors are logged and skipped
    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)

    flow.refresh.assert_awaited_once_with(c.id)
    mgr.update_oauth_token.assert_not_called()
    mgr.remove_oauth_server.assert_not_awaited()


async def test_refresh_job_permanent_error_removes_server(factory):
    key = generate_key().encode()
    async with factory() as s:
        c = await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                              runtime_name="calendar:w")
        await MCPConnectionRepo(s).set_tokens(
            c.id, access_token_enc=encrypt_blob(b"old", key), refresh_token_enc=encrypt_blob(b"r", key),
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30), scopes_granted=[])

    flow = MagicMock()
    flow.refresh = AsyncMock(side_effect=OAuthRefreshPermanentError("invalid_grant"))
    mgr = MagicMock()
    mgr.update_oauth_token = MagicMock()
    mgr.remove_oauth_server = AsyncMock()

    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)

    flow.refresh.assert_awaited_once_with(c.id)
    mgr.update_oauth_token.assert_not_called()
    mgr.remove_oauth_server.assert_awaited_once_with("calendar:w")


async def test_refresh_job_times_out_hung_attach_fallback(factory, monkeypatch):
    from jarvis.scheduler import oauth_jobs

    monkeypatch.setattr(oauth_jobs, "OAUTH_REFRESH_ATTACH_TIMEOUT", 0.01)
    key = generate_key().encode()
    async with factory() as s:
        c = await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                              runtime_name="calendar:w")
        await MCPConnectionRepo(s).set_tokens(
            c.id, access_token_enc=encrypt_blob(b"old", key), refresh_token_enc=encrypt_blob(b"r", key),
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30), scopes_granted=[])

    flow = MagicMock()
    flow.refresh = AsyncMock(return_value={"Authorization": "Bearer AT-NEW"})

    mgr = MagicMock()
    mgr.update_oauth_token = MagicMock(return_value=False)  # not attached — force full attach

    async def _hanging_replace(runtime_name, *, url, headers):
        await asyncio.Event().wait()  # hangs forever

    mgr.replace_oauth_server = AsyncMock(side_effect=_hanging_replace)
    mgr._catalog = MagicMock()
    mgr._catalog.get = AsyncMock(return_value=MagicMock(mcp_url="https://calendarmcp.googleapis.com/mcp/v1"))

    # Should return without hanging even though replace_oauth_server hangs
    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)

    mgr.update_oauth_token.assert_called_once_with("calendar:w", "AT-NEW")
    mgr.replace_oauth_server.assert_awaited_once()


async def test_pending_sweep_removes_old_rows(factory):
    now = datetime.now(UTC)
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                                 runtime_name="calendar:w")
        repo = MCPPendingRepo(s)
        await repo.insert(
            state="old",
            connection_id=conn.id,
            code_verifier="v",
            now=now - timedelta(hours=2),
        )
        await repo.insert(
            state="new",
            connection_id=conn.id,
            code_verifier="v",
            now=now - timedelta(seconds=10),
        )
    n = await oauth_pending_sweep(session_factory=factory, ttl_seconds=600)
    assert n == 1
    async with factory() as s:
        assert await MCPPendingRepo(s).get("old") is None
        assert await MCPPendingRepo(s).get("new") is not None

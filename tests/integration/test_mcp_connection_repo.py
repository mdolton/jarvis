from datetime import UTC, datetime, timedelta

import pytest_asyncio

from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import MCPProviderRow


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = session_factory(engine)
    # seed the FK parent so MCPConnectionRow inserts don't violate the FK constraint
    now = datetime.now(UTC)
    async with sf() as s:
        s.add(MCPProviderRow(
            key="calendar", display_name="Google Calendar", kind="oauth",
            mcp_url="https://example.com/mcp", extra_auth_params={},
            default_scopes=[], header_names=[], created_at=now, updated_at=now,
        ))
        await s.commit()
    yield sf
    await engine.dispose()


async def test_create_get_and_set_tokens(factory):
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="calendar", label="Work", runtime_name="calendar:work",
            client_id_enc=b"cid", client_secret_enc=b"sec", scopes=["a"],
        )
        cid = conn.id
    async with factory() as s:
        await MCPConnectionRepo(s).set_tokens(
            cid, access_token_enc=b"tok", refresh_token_enc=b"ref",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1), scopes_granted=["a"],
        )
    async with factory() as s:
        got = await MCPConnectionRepo(s).get(cid)
        assert got.access_token_enc == b"tok"
        assert got.status == "connected"


async def test_list_due_for_refresh_filters_by_status_and_expiry(factory):
    soon = datetime.now(UTC) + timedelta(seconds=30)
    async with factory() as s:
        repo = MCPConnectionRepo(s)
        c = await repo.create(provider_key="calendar", label="W", runtime_name="calendar:w")
        await repo.set_tokens(c.id, access_token_enc=b"t", refresh_token_enc=b"r",
                              token_expires_at=soon, scopes_granted=[])
    async with factory() as s:
        due = await MCPConnectionRepo(s).list_due_for_refresh(now=datetime.now(UTC), skew_seconds=90)
        assert [d.runtime_name for d in due] == ["calendar:w"]

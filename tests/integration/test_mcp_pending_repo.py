from datetime import UTC, datetime

import pytest_asyncio

from jarvis.oauth.store import MCPConnectionRepo, MCPPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import MCPProviderRow


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = session_factory(engine)
    # seed provider + connection to satisfy the FK constraint (PRAGMA foreign_keys=ON)
    now = datetime.now(UTC)
    async with sf() as s:
        s.add(MCPProviderRow(
            key="test-provider", display_name="Test Provider", kind="oauth",
            mcp_url="https://example.com/mcp", extra_auth_params={},
            default_scopes=[], header_names=[], created_at=now, updated_at=now,
        ))
        await s.commit()
    async with sf() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="test-provider", label="Test", runtime_name="test-provider:default",
        )
        connection_id = conn.id
    yield sf, connection_id
    await engine.dispose()


async def test_pending_keyed_by_connection_id(factory):
    sf, cid = factory
    async with sf() as s:
        await MCPPendingRepo(s).insert(state="st", connection_id=cid,
                                       code_verifier="v", now=datetime.now(UTC))
    async with sf() as s:
        row = await MCPPendingRepo(s).get("st")
        assert row.connection_id == cid
        assert row.code_verifier == "v"

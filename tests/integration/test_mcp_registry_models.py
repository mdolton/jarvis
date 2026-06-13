"""mcp_providers / mcp_connections ORM rows persist and relate."""
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import MCPConnectionRow, MCPProviderRow


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_provider_with_connections_round_trips(factory):
    now = datetime.now(UTC)
    async with factory() as s:
        s.add(MCPProviderRow(
            key="calendar", display_name="Google Calendar", kind="oauth",
            mcp_url="https://calendarmcp.googleapis.com/mcp/v1", builtin=True,
            auth_mode="manual", oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            default_scopes=["a", "b"], created_at=now, updated_at=now,
        ))
        s.add(MCPConnectionRow(
            provider_key="calendar", label="Work", runtime_name="calendar:work",
            scopes=["a"], created_at=now, updated_at=now,
        ))
        await s.commit()

    async with factory() as s:
        prov = (await s.execute(select(MCPProviderRow))).scalar_one()
        conns = (await s.execute(select(MCPConnectionRow))).scalars().all()
        assert prov.key == "calendar"
        assert prov.builtin is True
        assert len(conns) == 1
        assert conns[0].runtime_name == "calendar:work"
        assert conns[0].access_token_enc is None  # not-yet-authorized sentinel

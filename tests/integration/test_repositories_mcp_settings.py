import pytest

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    MCPServerRepo,
    MCPToolRepo,
    SettingsRepo,
)


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_mcp_server_upsert_by_name(session):
    repo = MCPServerRepo(session)
    s1 = await repo.upsert(name="gcal", transport="stdio")
    s2 = await repo.upsert(name="gcal", transport="stdio")
    assert s1.id == s2.id


async def test_mcp_server_status_update(session):
    repo = MCPServerRepo(session)
    s = await repo.upsert(name="gcal", transport="stdio")
    await repo.set_status(s.id, status="connected", last_error=None)

    listed = await repo.list_all()
    assert listed[0].status == "connected"


async def test_mcp_tool_replace_for_server(session):
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    server = await srepo.upsert(name="gcal", transport="stdio")

    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(
                name="list_events",
                input_schema={},
                read_only_hint=True,
                destructive_hint=False,
            ),
            MCPToolDescriptor(
                name="create_event",
                input_schema={},
                read_only_hint=False,
                destructive_hint=False,
            ),
        ],
    )
    got = await trepo.list_for_server(server.id)
    assert {t.name for t in got} == {"list_events", "create_event"}

    # Replacing with a smaller set removes the old rows.
    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(
                name="list_events",
                input_schema={},
                read_only_hint=True,
                destructive_hint=False,
            ),
        ],
    )
    got = await trepo.list_for_server(server.id)
    assert {t.name for t in got} == {"list_events"}


async def test_settings_get_set(session):
    repo = SettingsRepo(session)
    assert await repo.get("missing") is None

    await repo.set("idle_timeout_sec", 1800)
    assert await repo.get("idle_timeout_sec") == 1800

    await repo.set("idle_timeout_sec", 600)
    assert await repo.get("idle_timeout_sec") == 600

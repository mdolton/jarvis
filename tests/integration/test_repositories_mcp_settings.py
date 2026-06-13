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


async def test_delete_stdio_absent_from_prunes_orphans_keeps_configured_and_connections(session):
    repo = MCPServerRepo(session)
    # Configured stdio server still present in yaml -> kept.
    await repo.upsert(name="ynab_live", transport="stdio")
    # Orphans left behind by old data model / removed-from-yaml servers -> pruned.
    await repo.upsert(name="gmail", transport="http")
    await repo.upsert(name="calendar", transport="http")
    await repo.upsert(name="ynab", transport="stdio")
    # Connection-backed row -> never touched, even though absent from yaml names.
    await repo.upsert(name="gmail:default", transport="http", source="connection")

    pruned = await repo.delete_stdio_absent_from(["ynab_live"])

    assert pruned == 3
    remaining = {s.name for s in await repo.list_all()}
    assert remaining == {"ynab_live", "gmail:default"}


async def test_delete_stdio_absent_from_cascades_tools(session):
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    orphan = await srepo.upsert(name="ynab", transport="stdio")
    await trepo.replace_for_server(
        orphan.id,
        tools=[MCPToolDescriptor(name="t", description="", input_schema={})],
    )

    await srepo.delete_stdio_absent_from([])

    assert await trepo.list_for_server(orphan.id) == []


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


async def test_mcp_tool_replace_preserves_policy_override(session):
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    server = await srepo.upsert(name="gcal", transport="stdio")

    # Initial replace establishes the tool with no override.
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

    # Simulate the user setting an override directly in the DB.
    from sqlalchemy import update

    from jarvis.persistence.models import MCPToolRow

    await session.execute(
        update(MCPToolRow)
        .where(MCPToolRow.server_id == server.id, MCPToolRow.name == "list_events")
        .values(policy_override="confirm")
    )
    await session.commit()

    # Reconnect — replace_for_server with the same descriptor should preserve
    # the override (because nothing about the tool definition itself changed).
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

    rows = await trepo.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].policy_override == "confirm"


async def test_mcp_tool_replace_drops_policy_when_tool_disappears(session):
    """If the server stops advertising a tool, the override goes with it."""
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    server = await srepo.upsert(name="gcal", transport="stdio")

    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(name="old_tool", input_schema={}),
        ],
    )

    from sqlalchemy import update

    from jarvis.persistence.models import MCPToolRow

    await session.execute(
        update(MCPToolRow)
        .where(MCPToolRow.server_id == server.id, MCPToolRow.name == "old_tool")
        .values(policy_override="auto")
    )
    await session.commit()

    # Replace with a different tool — old_tool's row (and its override) is gone.
    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(name="new_tool", input_schema={}),
        ],
    )

    rows = await trepo.list_for_server(server.id)
    assert {r.name for r in rows} == {"new_tool"}
    assert rows[0].policy_override is None


async def test_mcp_tool_repo_sets_and_clears_policy_override(session):
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    server = await srepo.upsert(name="gcal", transport="stdio")
    await trepo.replace_for_server(
        server.id,
        tools=[MCPToolDescriptor(name="create_event", input_schema={})],
    )
    tool = (await trepo.list_for_server(server.id))[0]

    await trepo.set_policy_override(tool.id, "confirm")
    updated = (await trepo.list_for_server(server.id))[0]
    assert updated.policy_override == "confirm"

    await trepo.set_policy_override(tool.id, None)
    cleared = (await trepo.list_for_server(server.id))[0]
    assert cleared.policy_override is None

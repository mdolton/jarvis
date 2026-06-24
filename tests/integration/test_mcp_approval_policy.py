import pytest
from sqlalchemy import event

from jarvis.mcp.approval_policy import MCPApprovalPolicy
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
async def engine_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield engine, factory
    await engine.dispose()


async def test_confirm_tools_need_approval(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="mail", transport="http")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="send_email", input_schema={})],
        )

    policy = MCPApprovalPolicy(session_factory=factory)

    assert await policy.needs_approval("mail", _tool("send_email")) is True


async def test_read_tools_do_not_need_approval(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="calendar", transport="http")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="list_events", input_schema={})],
        )

    policy = MCPApprovalPolicy(session_factory=factory)

    assert await policy.needs_approval("calendar", _tool("list_events")) is False
    assert await policy.filter_tool("calendar", _tool("list_events")) is True


async def test_deny_override_is_filtered_out(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="calendar", transport="http")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="delete_event", input_schema={})],
        )
        tool = (await MCPToolRepo(session).list_for_server(server.id))[0]
        await MCPToolRepo(session).set_policy_override(tool.id, "deny")

    policy = MCPApprovalPolicy(session_factory=factory)

    assert await policy.filter_tool("calendar", _tool("delete_event")) is False
    assert await policy.needs_approval("calendar", _tool("delete_event")) is False
    assert await policy.is_denied("calendar", _tool("delete_event")) is True
    assert await policy.is_denied("calendar", "delete_event") is True


async def test_allow_override_skips_approval_and_remains_visible(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="gmail", transport="http")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="send_email", input_schema={})],
        )
        tool = (await MCPToolRepo(session).list_for_server(server.id))[0]
        await MCPToolRepo(session).set_policy_override(tool.id, "allow")

    policy = MCPApprovalPolicy(session_factory=factory)

    assert await policy.needs_approval("gmail", _tool("send_email")) is False
    assert await policy.filter_tool("gmail", _tool("send_email")) is True


async def test_policy_cache_reuses_server_lookup_and_can_be_invalidated(engine_and_factory):
    engine, factory = engine_and_factory
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="calendar", transport="http")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[
                MCPToolDescriptor(name="list_events", input_schema={}),
                MCPToolDescriptor(name="send_email", input_schema={}),
            ],
        )

    statements = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count_tool_selects(conn, cursor, statement, parameters, context, executemany):
        if "FROM mcp_tools" in statement and "mcp_servers" in statement:
            statements.append(statement)

    policy = MCPApprovalPolicy(session_factory=factory)

    assert await policy.filter_tool("calendar", _tool("list_events")) is True
    assert await policy.needs_approval("calendar", _tool("send_email")) is True
    assert len(statements) == 1

    async with factory() as session:
        tool = (await MCPToolRepo(session).list_for_server(server.id))[0]
        await MCPToolRepo(session).set_policy_override(tool.id, "deny")

    assert await policy.filter_tool("calendar", _tool("list_events")) is True
    assert len(statements) == 1

    policy.clear_server("calendar")

    assert await policy.filter_tool("calendar", _tool("list_events")) is False
    assert len(statements) == 2


async def test_missing_policy_row_falls_back_to_sdk_annotations(factory):
    policy = MCPApprovalPolicy(session_factory=factory)
    tool = _tool(
        "send_email",
        description="Send email",
        read_only_hint=True,
        destructive_hint=False,
    )

    descriptor, override = await policy._lookup("missing", tool)

    assert override is None
    assert descriptor.description == "Send email"
    assert descriptor.read_only_hint is True
    assert descriptor.destructive_hint is False
    assert await policy.needs_approval("missing", tool) is False


async def test_set_policy_override_for_server_bulk(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="brave", transport="stdio")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[
                MCPToolDescriptor(name="brave_web_search", input_schema={}),
                MCPToolDescriptor(name="brave_local_search", input_schema={}),
            ],
        )

    async with factory() as session:
        await MCPToolRepo(session).set_policy_override_for_server(server.id, "allow")

    async with factory() as session:
        tools = await MCPToolRepo(session).list_for_server(server.id)
    assert {t.name: t.policy_override for t in tools} == {
        "brave_web_search": "allow",
        "brave_local_search": "allow",
    }


async def test_allow_override_flips_needs_approval_for_stdio_tool(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="brave", transport="stdio")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="brave_web_search", input_schema={})],
        )
        tool_id = (await MCPToolRepo(session).list_for_server(server.id))[0].id

    policy = MCPApprovalPolicy(session_factory=factory)
    # Non-read-prefixed, no hints -> defaults to CONFIRM.
    assert await policy.needs_approval("brave", _tool("brave_web_search")) is True

    async with factory() as session:
        await MCPToolRepo(session).set_policy_override(tool_id, "allow")
    policy.clear_server("brave")

    assert await policy.needs_approval("brave", _tool("brave_web_search")) is False


def _tool(
    name: str,
    *,
    description: str = "",
    read_only_hint: bool | None = None,
    destructive_hint: bool | None = None,
):
    class Annotations:
        def __init__(self):
            self.readOnlyHint = read_only_hint
            self.destructiveHint = destructive_hint

    class Tool:
        def __init__(self, name):
            self.name = name
            self.inputSchema = {}
            self.description = description
            self.annotations = Annotations()

    return Tool(name)

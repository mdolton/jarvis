import pytest

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


def _tool(name: str):
    class Tool:
        def __init__(self, name):
            self.name = name
            self.inputSchema = {}
            self.description = ""
            self.annotations = None

    return Tool(name)

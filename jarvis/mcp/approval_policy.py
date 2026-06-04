"""Runtime MCP approval and filtering policy."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.tool_policy import RuntimeToolDecision, runtime_decision
from jarvis.persistence.models import MCPServerRow, MCPToolRow


class MCPApprovalPolicy:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def needs_approval(self, server_name: str, tool: Any) -> bool:
        descriptor, override = await self._lookup(server_name, tool)
        return runtime_decision(descriptor, override=override) == RuntimeToolDecision.CONFIRM

    async def filter_tool(self, server_name: str, tool: Any) -> bool:
        descriptor, override = await self._lookup(server_name, tool)
        return runtime_decision(descriptor, override=override) != RuntimeToolDecision.DENY

    async def _lookup(self, server_name: str, tool: Any) -> tuple[MCPToolDescriptor, str | None]:
        tool_name = tool.name
        async with self._session_factory() as session:
            result = await session.execute(
                select(MCPToolRow)
                .join(MCPServerRow)
                .where(MCPServerRow.name == server_name, MCPToolRow.name == tool_name)
            )
            row = result.scalar_one_or_none()

        if row is None:
            return _descriptor_from_sdk_tool(tool), None

        return (
            MCPToolDescriptor(
                name=row.name,
                description=row.description,
                input_schema=row.input_schema,
                read_only_hint=row.read_only_hint,
                destructive_hint=row.destructive_hint,
            ),
            row.policy_override,
        )


def _descriptor_from_sdk_tool(tool: Any) -> MCPToolDescriptor:
    return MCPToolDescriptor(
        name=tool.name,
        input_schema=dict(getattr(tool, "inputSchema", None) or {}),
    )

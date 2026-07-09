"""Runtime MCP approval and filtering policy."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.core.run_scope import current_trigger_source
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.tool_policy import RuntimeToolDecision, runtime_decision
from jarvis.persistence.models import MCPServerRow, MCPToolRow


class MCPApprovalPolicy:
    """Runtime policy decisions, evaluated under the current trigger scope.

    Each decision reads `current_trigger_source` so scheduled/event turns get
    the restricted (read-only) tool scope. The SDK applies `tool_filter` on
    every list_tools call (its cache holds the raw list), so per-run filtering
    composes with `cache_tools_list=True`.
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._cache: dict[str, dict[str, tuple[MCPToolDescriptor, str | None]]] = {}

    async def needs_approval(self, server_name: str, tool: Any) -> bool:
        return await self._decide(server_name, tool) == RuntimeToolDecision.CONFIRM

    async def filter_tool(self, server_name: str, tool: Any) -> bool:
        return await self._decide(server_name, tool) != RuntimeToolDecision.DENY

    async def is_denied(self, server_name: str, tool_or_name: Any) -> bool:
        tool = _tool_from_name(tool_or_name) if isinstance(tool_or_name, str) else tool_or_name
        return await self._decide(server_name, tool) == RuntimeToolDecision.DENY

    async def _decide(self, server_name: str, tool: Any) -> RuntimeToolDecision:
        descriptor, override = await self._lookup(server_name, tool)
        return runtime_decision(
            descriptor,
            override=override,
            trigger_source=current_trigger_source.get(),
        )

    def clear_cache(self) -> None:
        self._cache.clear()

    def clear_server(self, server_name: str) -> None:
        self._cache.pop(server_name, None)

    async def _lookup(self, server_name: str, tool: Any) -> tuple[MCPToolDescriptor, str | None]:
        tool_name = tool.name
        server_tools = await self._tools_for_server(server_name)
        cached = server_tools.get(tool_name)
        if cached is not None:
            return cached

        return _descriptor_from_sdk_tool(tool), None

    async def _tools_for_server(
        self,
        server_name: str,
    ) -> dict[str, tuple[MCPToolDescriptor, str | None]]:
        cached = self._cache.get(server_name)
        if cached is not None:
            return cached

        async with self._session_factory() as session:
            result = await session.execute(
                select(MCPToolRow).join(MCPServerRow).where(MCPServerRow.name == server_name)
            )
            rows = list(result.scalars())

        server_tools = {
            row.name: (
                MCPToolDescriptor(
                    name=row.name,
                    description=row.description,
                    input_schema=row.input_schema,
                    read_only_hint=row.read_only_hint,
                    destructive_hint=row.destructive_hint,
                ),
                row.policy_override,
            )
            for row in rows
        }
        self._cache[server_name] = server_tools
        return server_tools


def _descriptor_from_sdk_tool(tool: Any) -> MCPToolDescriptor:
    annotations = getattr(tool, "annotations", None)
    return MCPToolDescriptor(
        name=tool.name,
        description=getattr(tool, "description", "") or "",
        input_schema=dict(getattr(tool, "inputSchema", None) or {}),
        read_only_hint=getattr(annotations, "readOnlyHint", None) if annotations else None,
        destructive_hint=getattr(annotations, "destructiveHint", None) if annotations else None,
    )


class _ToolName:
    name: str
    inputSchema: dict
    description: str = ""
    annotations = None

    def __init__(self, name: str) -> None:
        self.name = name
        self.inputSchema = {}


def _tool_from_name(name: str) -> _ToolName:
    return _ToolName(name)

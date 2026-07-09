"""Runtime MCP approval and filtering policy."""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.core.run_scope import current_trigger_source
from jarvis.core.types import AuditEvent, AuditEventType, TriggerSource
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.sensitivity import find_sensitive_match
from jarvis.mcp.tool_policy import RuntimeToolDecision, classify_effect, runtime_decision
from jarvis.persistence.models import MCPServerRow, MCPToolRow

_log = logging.getLogger(__name__)

SensitivityTermsProvider = Callable[[], Awaitable[Sequence[str]]]


class MCPApprovalPolicy:
    """Runtime policy decisions, evaluated under the current trigger scope.

    Each decision reads `current_trigger_source` so scheduled/event turns get
    the restricted (read-only) tool scope. The SDK applies `tool_filter` on
    every list_tools call (its cache holds the raw list), so per-run filtering
    composes with `cache_tools_list=True`.

    `needs_approval` is the per-call gate: it sees the call arguments (wired
    through the runtime policy guard in mcp/manager.py), escalates on
    sensitivity-term matches, and records every decision — auto-allowed or
    gated — as a `tool.policy_decision` audit event.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: Any = None,
        sensitivity_terms_provider: SensitivityTermsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._sensitivity_terms_provider = sensitivity_terms_provider
        self._cache: dict[str, dict[str, tuple[MCPToolDescriptor, str | None]]] = {}

    async def needs_approval(
        self,
        server_name: str,
        tool: Any,
        arguments: dict | None = None,
        call_id: str | None = None,
    ) -> bool:
        descriptor, override = await self._lookup(server_name, tool)
        sensitive_term = await self._sensitive_term(descriptor.name, arguments)
        trigger_source = current_trigger_source.get()
        decision = runtime_decision(
            descriptor,
            override=override,
            trigger_source=trigger_source,
            sensitive=sensitive_term is not None,
        )
        await self._emit_decision(
            server_name=server_name,
            descriptor=descriptor,
            override=override,
            trigger_source=trigger_source,
            decision=decision,
            sensitive_term=sensitive_term,
            call_id=call_id,
        )
        return decision == RuntimeToolDecision.CONFIRM

    async def _sensitive_term(self, tool_name: str, arguments: dict | None) -> str | None:
        if self._sensitivity_terms_provider is None:
            return None
        try:
            terms = await self._sensitivity_terms_provider()
        except Exception:
            # Fail open on the *sensitivity* signal only — the effect
            # classification still gates irreversible calls.
            _log.warning("sensitivity terms provider failed", exc_info=True)
            return None
        return find_sensitive_match(terms, tool_name=tool_name, arguments=arguments)

    async def _emit_decision(
        self,
        *,
        server_name: str,
        descriptor: MCPToolDescriptor,
        override: str | None,
        trigger_source: TriggerSource,
        decision: RuntimeToolDecision,
        sensitive_term: str | None,
        call_id: str | None,
    ) -> None:
        if self._audit is None:
            return
        payload: dict[str, Any] = {
            "server_name": server_name,
            "tool_name": descriptor.name,
            "decision": decision.value,
            "effect": classify_effect(descriptor).value,
            "trigger_source": trigger_source.value,
            "auto_allowed": decision == RuntimeToolDecision.ALLOW,
        }
        if override is not None:
            payload["override"] = override
        if sensitive_term is not None:
            payload["sensitive_term"] = sensitive_term
        if call_id is not None:
            payload["call_id"] = call_id
        try:
            await self._audit.emit(
                AuditEvent(type=AuditEventType.TOOL_POLICY_DECISION, payload=payload)
            )
        except Exception:
            _log.warning("failed to emit tool policy decision audit event", exc_info=True)

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

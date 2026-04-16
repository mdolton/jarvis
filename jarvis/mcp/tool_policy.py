"""Tool policy classifier.

Decision precedence (highest to lowest):
  1. User override (from the mcp_tools.policy_override column).
  2. MCP annotations: destructive_hint=True → confirm; read_only_hint=True → auto.
  3. Heuristic on the tool name: read-like prefixes → auto; otherwise → confirm.

The function is pure; callers pass the override explicitly. The classification
is recorded on every `tool.call` audit event even in v1 (where all tools run),
so the v2 confirmation flow is a UI change rather than a design change.
"""

from enum import StrEnum

from jarvis.mcp.descriptor import MCPToolDescriptor

_READ_PREFIXES = ("get_", "list_", "read_", "search_", "fetch_")


class ToolPolicy(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"


def classify(
    tool: MCPToolDescriptor,
    *,
    override: str | None = None,
) -> ToolPolicy:
    """Decide whether `tool` auto-executes or requires confirmation."""
    if override in ("auto", "confirm"):
        return ToolPolicy(override)

    if tool.destructive_hint is True:
        return ToolPolicy.CONFIRM
    if tool.read_only_hint is True:
        return ToolPolicy.AUTO

    name_lower = tool.name.lower()
    if any(name_lower.startswith(p) for p in _READ_PREFIXES):
        return ToolPolicy.AUTO
    return ToolPolicy.CONFIRM

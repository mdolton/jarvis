"""Tool policy classifier.

Decision precedence (highest to lowest):
  1. Trigger source: non-user turns (scheduled/event) may only run strictly
     read-only tools — everything else is denied, even with an allow override.
     Untrusted inbound content (email bodies, calendar invites) reaches the
     model on those turns, so an injected instruction must not be able to
     drive a side-effecting tool call (OWASP LLM01).
  2. User override (from the mcp_tools.policy_override column).
  3. MCP annotations: destructive_hint=True → confirm; read_only_hint=True → auto.
  4. Heuristic on the tool name: read-like prefixes → auto; otherwise → confirm.

The functions are pure; callers pass the override and trigger source
explicitly. The classification is recorded on every `tool.call` audit event
even in v1 (where all tools run), so the v2 confirmation flow is a UI change
rather than a design change.
"""

from enum import StrEnum

from jarvis.core.types import TriggerSource
from jarvis.mcp.descriptor import MCPToolDescriptor

_READ_PREFIXES = ("get_", "list_", "read_", "search_", "fetch_")


class ToolPolicy(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"


class RuntimeToolDecision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


def classify(
    tool: MCPToolDescriptor,
    *,
    override: str | None = None,
) -> ToolPolicy:
    """Decide whether `tool` auto-executes or requires confirmation."""
    if override in ("auto", "allow", "confirm"):
        if override == "allow":
            return ToolPolicy.AUTO
        return ToolPolicy(override)

    if tool.destructive_hint is True:
        return ToolPolicy.CONFIRM
    if tool.read_only_hint is True:
        return ToolPolicy.AUTO

    name_lower = tool.name.lower()
    if any(name_lower.startswith(p) for p in _READ_PREFIXES):
        return ToolPolicy.AUTO
    return ToolPolicy.CONFIRM


def is_read_only(tool: MCPToolDescriptor) -> bool:
    """Strictly read-only: not destructive, and annotated or named as a read."""
    if tool.destructive_hint is True:
        return False
    if tool.read_only_hint is True:
        return True
    name_lower = tool.name.lower()
    return any(name_lower.startswith(p) for p in _READ_PREFIXES)


def runtime_decision(
    tool: MCPToolDescriptor,
    *,
    override: str | None = None,
    trigger_source: TriggerSource = TriggerSource.USER,
) -> RuntimeToolDecision:
    """Decide whether `tool` is allowed, requires approval, or is hidden."""
    if override == "deny":
        return RuntimeToolDecision.DENY
    if trigger_source != TriggerSource.USER and not is_read_only(tool):
        return RuntimeToolDecision.DENY
    if override in ("allow", "confirm"):
        return RuntimeToolDecision(override)

    policy = classify(tool, override=override)
    if policy == ToolPolicy.AUTO:
        return RuntimeToolDecision.ALLOW
    return RuntimeToolDecision.CONFIRM

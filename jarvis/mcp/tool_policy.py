"""Tool policy classifier.

Approval model: auto-allow reversible / low-blast-radius calls, gate only
irreversible, outward-facing, or sensitive ones. High-volume confirm gates
collapse into rubber-stamping; fewer, better-targeted gates are more real
oversight (arXiv:2510.04465).

Decision precedence (highest to lowest):
  1. Trigger source: non-user turns (scheduled/event) may only run strictly
     read-only tools — everything else is denied, even with an allow override.
     Untrusted inbound content (email bodies, calendar invites) reaches the
     model on those turns, so an injected instruction must not be able to
     drive a side-effecting tool call (OWASP LLM01).
  2. User deny override (from the mcp_tools.policy_override column).
  3. Sensitivity: a call whose arguments touch a known-sensitive term
     escalates to confirm. Escalate-only — it never relaxes an outcome.
  4. User allow/confirm override.
  5. MCP annotations: destructive_hint=True → confirm; read_only_hint=True → auto.
  6. Effect heuristic on the leading verb of the tool name: reads and
     reversible mutations → auto; irreversible/outward effects and unknown
     verbs → confirm.

Classification is by *effect*, not read-vs-write: a draft-create is
reversible (auto), a send is not (confirm). Unknown verbs inherit the safe
default (confirm) so new tools need no allowlist entry.

The functions are pure; callers pass the override, trigger source, and
sensitivity flag explicitly. The decision is recorded on a
`tool.policy_decision` audit event by MCPApprovalPolicy.
"""

import re
from enum import StrEnum

from jarvis.core.types import TriggerSource
from jarvis.mcp.descriptor import MCPToolDescriptor

# Leading verbs that only observe state.
_READ_VERBS = frozenset(
    {
        "get",
        "list",
        "read",
        "search",
        "fetch",
        "find",
        "query",
        "describe",
        "lookup",
        "view",
        "show",
        "check",
        "count",
        "preview",
        "download",
        "suggest",
        "compare",
    }
)

# Leading verbs that mutate state the agent's owner can undo (contained
# blast radius): drafts, labels, calendar entries, library items, flags.
_REVERSIBLE_VERBS = frozenset(
    {
        "create",
        "add",
        "update",
        "set",
        "edit",
        "modify",
        "rename",
        "move",
        "copy",
        "save",
        "draft",
        "upsert",
        "insert",
        "append",
        "apply",
        "label",
        "unlabel",
        "tag",
        "untag",
        "mark",
        "unmark",
        "star",
        "unstar",
        "archive",
        "unarchive",
        "mute",
        "unmute",
        "pin",
        "unpin",
        "snooze",
        "toggle",
        "enable",
        "disable",
        "upload",
        "import",
        "restore",
        "complete",
        "reopen",
        "favorite",
        "unfavorite",
    }
)

# Leading verbs whose effect either leaves the owner's boundary (another
# person or system observes it) or cannot be undone.
_IRREVERSIBLE_VERBS = frozenset(
    {
        "send",
        "reply",
        "forward",
        "publish",
        "post",
        "share",
        "submit",
        "pay",
        "purchase",
        "buy",
        "order",
        "transfer",
        "execute",
        "run",
        "invoke",
        "delete",
        "remove",
        "destroy",
        "drop",
        "purge",
        "erase",
        "wipe",
        "revoke",
        "cancel",
        "respond",
        "accept",
        "decline",
        "reject",
        "approve",
        "notify",
        "broadcast",
        "invite",
        "grant",
        "sign",
        "deploy",
        "merge",
        "push",
        "release",
        "charge",
        "refund",
        "book",
    }
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class ToolEffect(StrEnum):
    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class ToolPolicy(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"


class RuntimeToolDecision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


def _leading_verb(name: str) -> str:
    snake = _CAMEL_BOUNDARY.sub("_", name).lower()
    for token in re.split(r"[^a-z]+", snake):
        if token:
            return token
    return ""


def classify_effect(tool: MCPToolDescriptor) -> ToolEffect:
    """Classify `tool` by blast radius + reversibility."""
    if tool.destructive_hint is True:
        return ToolEffect.IRREVERSIBLE
    if tool.read_only_hint is True:
        return ToolEffect.READ

    verb = _leading_verb(tool.name)
    if verb in _IRREVERSIBLE_VERBS:
        return ToolEffect.IRREVERSIBLE
    if verb in _READ_VERBS:
        return ToolEffect.READ
    if verb in _REVERSIBLE_VERBS:
        return ToolEffect.REVERSIBLE
    return ToolEffect.UNKNOWN


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

    effect = classify_effect(tool)
    if effect in (ToolEffect.READ, ToolEffect.REVERSIBLE):
        return ToolPolicy.AUTO
    return ToolPolicy.CONFIRM


def is_read_only(tool: MCPToolDescriptor) -> bool:
    """Strictly read-only — the only effect class allowed on non-user turns."""
    return classify_effect(tool) == ToolEffect.READ


def runtime_decision(
    tool: MCPToolDescriptor,
    *,
    override: str | None = None,
    trigger_source: TriggerSource = TriggerSource.USER,
    sensitive: bool = False,
) -> RuntimeToolDecision:
    """Decide whether `tool` is allowed, requires approval, or is hidden."""
    if override == "deny":
        return RuntimeToolDecision.DENY
    if trigger_source != TriggerSource.USER and not is_read_only(tool):
        return RuntimeToolDecision.DENY
    if sensitive:
        return RuntimeToolDecision.CONFIRM
    if override in ("allow", "confirm"):
        return RuntimeToolDecision(override)

    policy = classify(tool, override=override)
    if policy == ToolPolicy.AUTO:
        return RuntimeToolDecision.ALLOW
    return RuntimeToolDecision.CONFIRM

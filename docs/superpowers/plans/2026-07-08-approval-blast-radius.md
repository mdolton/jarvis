# Blast-Radius Approval Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shift the approval default from "confirm unless read-only" to "auto-allow reversible / low-blast-radius, gate only irreversible or sensitive," with every auto-allowed decision recorded in the audit log.

**Architecture:** A pure effect classifier (`ToolEffect`: read / reversible / irreversible / unknown) replaces the read-prefix heuristic in `jarvis/mcp/tool_policy.py`. `MCPApprovalPolicy.needs_approval` gains tool-call arguments (via a patched `_get_needs_approval_for_tool` in the existing runtime-policy guard), matches them against sensitivity terms derived from memory preferences, and emits a `tool.policy_decision` audit event for every decision. The non-user-trigger read-only restriction (prompt-injection defense) is unchanged: reversible ≠ read-only.

**Tech Stack:** Python 3.12, openai-agents SDK, SQLAlchemy async + SQLite, pytest (`asyncio_mode = auto`).

## Global Constraints

- Persistence goes through repositories only.
- Non-user turns (scheduled/event) may still only run strictly READ tools — reversible mutations stay DENIED there (OWASP LLM01 defense from PR #55).
- Sensitivity can only escalate (allow → confirm), never relax an outcome; it never un-denies.
- Unknown effect → confirm (safe default; no brittle allowlist — new tools inherit this).
- Audit emission must be best-effort: a failed emit must never break a tool call.
- `make check` (ruff + pytest) green before PR.
- Branch off `main`; commits carry `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Effect-based classifier in tool_policy.py

**Files:**
- Modify: `jarvis/mcp/tool_policy.py` (full rewrite of classification internals)
- Test: `tests/unit/test_tool_policy.py`

**Interfaces:**
- Produces: `ToolEffect` StrEnum (READ/REVERSIBLE/IRREVERSIBLE/UNKNOWN); `classify_effect(tool: MCPToolDescriptor) -> ToolEffect`; `classify(tool, *, override=None) -> ToolPolicy` (unchanged signature); `runtime_decision(tool, *, override=None, trigger_source=TriggerSource.USER, sensitive: bool = False) -> RuntimeToolDecision` (new `sensitive` kwarg, default False so all existing callers are unaffected); `is_read_only(tool)` (unchanged signature, now `classify_effect(tool) == ToolEffect.READ`).

- [ ] **Step 1: Update/extend the unit tests to the new expectations**

Rewrite `tests/unit/test_tool_policy.py`. Keep all existing tests that still hold; change/add these:

```python
from jarvis.core.types import TriggerSource
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.tool_policy import (
    RuntimeToolDecision,
    ToolEffect,
    ToolPolicy,
    classify,
    classify_effect,
    runtime_decision,
)


def _desc(**kwargs) -> MCPToolDescriptor:
    defaults = {"name": "x", "input_schema": {}}
    defaults.update(kwargs)
    return MCPToolDescriptor(**defaults)


# --- classify_effect ---

def test_effect_reads():
    for name in ("get_thing", "list_things", "read_item", "search_docs", "fetch_url",
                 "find_events", "query_db", "describe_table", "lookup_contact",
                 "check_status", "download_file_content"):
        assert classify_effect(_desc(name=name)) == ToolEffect.READ, name


def test_effect_reversible_mutations():
    for name in ("create_draft", "create_event", "add_to_library", "update_event",
                 "set_reminder", "edit_note", "rename_file", "move_message",
                 "copy_file", "label_message", "unlabel_thread", "archive_thread",
                 "mark_read", "star_message", "upsert_row", "upload_image",
                 "restore_item", "apply_sensitive_message_label"):
        assert classify_effect(_desc(name=name)) == ToolEffect.REVERSIBLE, name


def test_effect_irreversible_or_outward():
    for name in ("send_email", "reply_to_thread", "forward_message", "publish_post",
                 "post_comment", "share_file", "submit_form", "pay_invoice",
                 "purchase_item", "order_pizza", "transfer_funds", "execute_query",
                 "run_script", "delete_event", "remove_from_library", "drop_table",
                 "purge_queue", "revoke_token", "cancel_subscription",
                 "respond_to_event", "accept_invite", "decline_meeting",
                 "notify_user", "invite_member", "grant_access", "sign_document"):
        assert classify_effect(_desc(name=name)) == ToolEffect.IRREVERSIBLE, name


def test_effect_unknown_names():
    for name in ("do_thing", "brave_web_search", "frobnicate"):
        assert classify_effect(_desc(name=name)) == ToolEffect.UNKNOWN, name


def test_effect_camel_case_and_bare_verbs():
    assert classify_effect(_desc(name="createDraft")) == ToolEffect.REVERSIBLE
    assert classify_effect(_desc(name="sendEmail")) == ToolEffect.IRREVERSIBLE
    assert classify_effect(_desc(name="GET_Thing")) == ToolEffect.READ
    assert classify_effect(_desc(name="fetch")) == ToolEffect.READ


def test_effect_hints_beat_names():
    # destructive_hint wins over a reversible-looking name.
    assert classify_effect(_desc(name="create_x", destructive_hint=True)) == ToolEffect.IRREVERSIBLE
    # read_only_hint wins over an irreversible-looking name.
    assert classify_effect(_desc(name="send_probe", read_only_hint=True)) == ToolEffect.READ
    # destructive beats read_only when both are set.
    assert (
        classify_effect(_desc(name="x", read_only_hint=True, destructive_hint=True))
        == ToolEffect.IRREVERSIBLE
    )


# --- classify (policy mapping) ---

def test_user_override_wins():
    t = _desc(name="whatever", destructive_hint=True)
    assert classify(t, override="auto") == ToolPolicy.AUTO
    assert classify(t, override="confirm") == ToolPolicy.CONFIRM


def test_reversible_mutations_auto_allow():
    for name in ("create_draft", "update_event", "add_label", "archive_thread"):
        assert classify(_desc(name=name)) == ToolPolicy.AUTO, name


def test_irreversible_and_unknown_confirm():
    for name in ("send_email", "delete_event", "pay_invoice", "do_thing"):
        assert classify(_desc(name=name)) == ToolPolicy.CONFIRM, name


# --- runtime_decision ---

def test_runtime_reversible_allows_irreversible_confirms():
    assert runtime_decision(_desc(name="create_draft")) == RuntimeToolDecision.ALLOW
    assert runtime_decision(_desc(name="send_email")) == RuntimeToolDecision.CONFIRM


def test_sensitive_escalates_reads_and_reversibles():
    assert (
        runtime_decision(_desc(name="get_message"), sensitive=True)
        == RuntimeToolDecision.CONFIRM
    )
    assert (
        runtime_decision(_desc(name="create_draft"), sensitive=True)
        == RuntimeToolDecision.CONFIRM
    )


def test_sensitive_beats_allow_override():
    t = _desc(name="create_draft")
    assert runtime_decision(t, override="allow", sensitive=True) == RuntimeToolDecision.CONFIRM


def test_sensitive_never_relaxes_deny():
    t = _desc(name="create_draft")
    assert runtime_decision(t, override="deny", sensitive=True) == RuntimeToolDecision.DENY


def test_non_user_source_still_denies_reversible_mutations():
    for source in (TriggerSource.SCHEDULED, TriggerSource.EVENT):
        for name in ("create_draft", "update_event", "archive_thread",
                     "send_email", "delete_event", "do_thing"):
            assert (
                runtime_decision(_desc(name=name), trigger_source=source)
                == RuntimeToolDecision.DENY
            ), (source, name)
```

Also keep (verbatim from the current file — they must still pass): `test_read_only_hint_auto`, `test_destructive_hint_confirm`, `test_destructive_wins_over_read_only`, `test_override_accepts_none`, `test_runtime_override_allow_confirm_deny`, `test_runtime_auto_detect_maps_classifier`, `test_non_user_source_denies_even_with_allow_or_confirm_override`, `test_non_user_source_denies_destructive_read_only`, `test_non_user_source_allows_read_only_tools`, `test_non_user_source_keeps_deny_override`, `test_user_source_behavior_unchanged`. Drop `test_heuristic_read_prefixes` / `test_heuristic_unknown_defaults_to_confirm` / `test_heuristic_case_insensitive` (superseded by the effect tests above).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tool_policy.py -q`
Expected: FAIL — `ImportError: cannot import name 'ToolEffect'`.

- [ ] **Step 3: Rewrite jarvis/mcp/tool_policy.py**

```python
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
_READ_VERBS = frozenset({
    "get", "list", "read", "search", "fetch", "find", "query", "describe",
    "lookup", "view", "show", "check", "count", "preview", "download",
    "suggest", "compare",
})

# Leading verbs that mutate state the agent's owner can undo (contained
# blast radius): drafts, labels, calendar entries, library items, flags.
_REVERSIBLE_VERBS = frozenset({
    "create", "add", "update", "set", "edit", "modify", "rename", "move",
    "copy", "save", "draft", "upsert", "insert", "append", "apply", "label",
    "unlabel", "tag", "untag", "mark", "unmark", "star", "unstar", "archive",
    "unarchive", "mute", "unmute", "pin", "unpin", "snooze", "toggle",
    "enable", "disable", "upload", "import", "restore", "complete", "reopen",
    "favorite", "unfavorite",
})

# Leading verbs whose effect either leaves the owner's boundary (another
# person or system observes it) or cannot be undone.
_IRREVERSIBLE_VERBS = frozenset({
    "send", "reply", "forward", "publish", "post", "share", "submit", "pay",
    "purchase", "buy", "order", "transfer", "execute", "run", "invoke",
    "delete", "remove", "destroy", "drop", "purge", "erase", "wipe",
    "revoke", "cancel", "respond", "accept", "decline", "reject", "approve",
    "notify", "broadcast", "invite", "grant", "sign", "deploy", "merge",
    "push", "release", "charge", "refund", "book",
})

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tool_policy.py tests/integration/test_mcp_approval_policy.py tests/integration/test_event_trigger_tool_scope.py -q`
Expected: PASS (approval-policy integration tests still hold: `send_email` confirms, `list_events` auto, `brave_web_search` confirms as UNKNOWN).

- [ ] **Step 5: Commit**

```bash
git add jarvis/mcp/tool_policy.py tests/unit/test_tool_policy.py
git commit -m "feat: classify tool calls by blast radius + reversibility"
```

---

### Task 2: Sensitivity terms — pure extraction and matching

**Files:**
- Create: `jarvis/mcp/sensitivity.py`
- Test: `tests/unit/test_sensitivity.py`

**Interfaces:**
- Produces: `extract_sensitivity_terms(preferences: Iterable[str]) -> list[str]` (lowercased, deduped, order-preserving); `find_sensitive_match(terms: Sequence[str], *, tool_name: str = "", arguments: dict | None = None) -> str | None` (returns the matched term or None).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_sensitivity.py`:

```python
from jarvis.mcp.sensitivity import extract_sensitivity_terms, find_sensitive_match


def test_extract_terms_from_marked_preferences():
    prefs = [
        "Prefers metric units",
        "sensitive: mom@example.com, Salary; therapist",
        "SENSITIVE: mom@example.com",
    ]
    assert extract_sensitivity_terms(prefs) == ["mom@example.com", "salary", "therapist"]


def test_extract_ignores_unmarked_and_empty():
    assert extract_sensitivity_terms(["no marker here", "sensitive:", ""]) == []


def test_match_in_nested_arguments():
    terms = ["mom@example.com", "salary"]
    args = {"to": [{"email": "Mom@Example.com"}], "body": "hi"}
    assert find_sensitive_match(terms, tool_name="create_draft", arguments=args) == (
        "mom@example.com"
    )


def test_match_in_tool_name_only():
    assert find_sensitive_match(["payroll"], tool_name="get_payroll_report") == "payroll"


def test_no_match_returns_none():
    assert find_sensitive_match(["salary"], tool_name="create_draft", arguments={"to": "x"}) is None
    assert find_sensitive_match([], tool_name="anything", arguments={"salary": 1}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_sensitivity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jarvis.mcp.sensitivity'`.

- [ ] **Step 3: Implement jarvis/mcp/sensitivity.py**

```python
"""Sensitivity signal for approval decisions.

Terms come from memory preferences of the form ``sensitive: a, b; c``
(case-insensitive marker, comma/semicolon-separated terms). A tool call
whose arguments or name contain any term escalates to confirm — the memory
layer makes escalation context-aware instead of a static tool list.
"""

import json
import re
from collections.abc import Iterable, Sequence

_MARKER = re.compile(r"^\s*sensitive\s*:\s*(?P<terms>.+)$", re.IGNORECASE | re.DOTALL)


def extract_sensitivity_terms(preferences: Iterable[str]) -> list[str]:
    """Collect sensitive terms from preference contents, lowercased and deduped."""
    terms: list[str] = []
    for content in preferences:
        match = _MARKER.match(content or "")
        if match is None:
            continue
        for raw in re.split(r"[,;]", match.group("terms")):
            term = raw.strip().lower()
            if term and term not in terms:
                terms.append(term)
    return terms


def find_sensitive_match(
    terms: Sequence[str],
    *,
    tool_name: str = "",
    arguments: dict | None = None,
) -> str | None:
    """Return the first term found in the tool name or serialized arguments."""
    if not terms:
        return None
    haystack = tool_name.lower()
    if arguments:
        try:
            haystack += " " + json.dumps(arguments, default=str).lower()
        except (TypeError, ValueError):
            haystack += " " + str(arguments).lower()
    for term in terms:
        if term in haystack:
            return term
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_sensitivity.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/mcp/sensitivity.py tests/unit/test_sensitivity.py
git commit -m "feat: sensitivity term extraction and argument matching"
```

---

### Task 3: Audited, sensitivity-aware needs_approval

**Files:**
- Modify: `jarvis/core/types.py` (add one enum member)
- Modify: `jarvis/mcp/approval_policy.py`
- Test: `tests/integration/test_mcp_approval_policy.py` (additions)

**Interfaces:**
- Consumes: `runtime_decision(..., sensitive=)`, `classify_effect` (Task 1), `find_sensitive_match` (Task 2).
- Produces: `AuditEventType.TOOL_POLICY_DECISION = "tool.policy_decision"`; `MCPApprovalPolicy.__init__(*, session_factory, audit=None, sensitivity_terms_provider=None)` where `sensitivity_terms_provider: Callable[[], Awaitable[Sequence[str]]] | None`; `MCPApprovalPolicy.needs_approval(server_name, tool, arguments: dict | None = None, call_id: str | None = None) -> bool`. Audit payload keys: `server_name`, `tool_name`, `decision`, `effect`, `trigger_source`, `auto_allowed` (+ optional `override`, `sensitive_term`, `call_id`).

- [ ] **Step 1: Write the failing tests (append to tests/integration/test_mcp_approval_policy.py)**

```python
class _StubAudit:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


async def test_reversible_tools_do_not_need_approval(factory):
    async with factory() as session:
        server = await MCPServerRepo(session).upsert(name="gmail", transport="http")
        await MCPToolRepo(session).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="create_draft", input_schema={})],
        )

    policy = MCPApprovalPolicy(session_factory=factory)

    assert await policy.needs_approval("gmail", _tool("create_draft")) is False


async def test_auto_allowed_decision_is_audited(factory):
    from jarvis.core.types import AuditEventType

    audit = _StubAudit()
    policy = MCPApprovalPolicy(session_factory=factory, audit=audit)

    assert await policy.needs_approval("gmail", _tool("create_draft")) is False

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.type == AuditEventType.TOOL_POLICY_DECISION
    assert event.payload["server_name"] == "gmail"
    assert event.payload["tool_name"] == "create_draft"
    assert event.payload["decision"] == "allow"
    assert event.payload["effect"] == "reversible"
    assert event.payload["auto_allowed"] is True


async def test_gated_decision_is_audited(factory):
    audit = _StubAudit()
    policy = MCPApprovalPolicy(session_factory=factory, audit=audit)

    assert await policy.needs_approval("gmail", _tool("send_email"), call_id="c1") is True

    event = audit.events[0]
    assert event.payload["decision"] == "confirm"
    assert event.payload["effect"] == "irreversible"
    assert event.payload["auto_allowed"] is False
    assert event.payload["call_id"] == "c1"


async def test_sensitive_arguments_escalate_to_approval(factory):
    async def terms():
        return ["mom@example.com"]

    audit = _StubAudit()
    policy = MCPApprovalPolicy(
        session_factory=factory, audit=audit, sensitivity_terms_provider=terms
    )

    needs = await policy.needs_approval(
        "gmail",
        _tool("create_draft"),
        arguments={"to": [{"email": "Mom@Example.com"}]},
    )
    assert needs is True
    assert audit.events[0].payload["sensitive_term"] == "mom@example.com"

    # Benign arguments still auto-allow.
    assert (
        await policy.needs_approval("gmail", _tool("create_draft"), arguments={"to": "x@y.z"})
        is False
    )


async def test_sensitivity_provider_failure_fails_open(factory):
    async def broken():
        raise RuntimeError("no terms for you")

    policy = MCPApprovalPolicy(session_factory=factory, sensitivity_terms_provider=broken)

    assert await policy.needs_approval("gmail", _tool("create_draft")) is False


async def test_audit_failure_does_not_break_decision(factory):
    class _BrokenAudit:
        async def emit(self, event):
            raise RuntimeError("audit down")

    policy = MCPApprovalPolicy(session_factory=factory, audit=_BrokenAudit())

    assert await policy.needs_approval("gmail", _tool("send_email")) is True
```

Also update the tail of `test_non_user_trigger_scope_denies_side_effect_tools` — it asserts `needs_approval("gmail", _tool("send_email")) is False` on a user turn because of the allow override; that still holds (no change needed, just verify).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_mcp_approval_policy.py -q`
Expected: new tests FAIL (`TypeError: unexpected keyword argument 'audit'` / missing enum member); old tests PASS.

- [ ] **Step 3: Add the enum member in jarvis/core/types.py**

After `TOOL_ERROR = "tool.error"` insert:

```python
    TOOL_POLICY_DECISION = "tool.policy_decision"
```

- [ ] **Step 4: Rework jarvis/mcp/approval_policy.py**

Replace the class body (keep `_descriptor_from_sdk_tool`, `_ToolName`, `_tool_from_name` as-is):

```python
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
        # ... unchanged ...
```

(`_tools_for_server` and the module-tail helpers stay byte-identical.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_mcp_approval_policy.py tests/unit/test_core_types.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/core/types.py jarvis/mcp/approval_policy.py tests/integration/test_mcp_approval_policy.py
git commit -m "feat: audit every tool policy decision; escalate on sensitive arguments"
```

---

### Task 4: Wire arguments, audit, and memory terms through manager/bootstrap

**Files:**
- Modify: `jarvis/mcp/manager.py` (`MCPManager.__init__`, `_apply_runtime_policy_guard`)
- Modify: `jarvis/main.py` (MCPManager construction)
- Modify: `jarvis/memory/service.py` (add `sensitivity_terms`)
- Test: `tests/integration/test_mcp_manager.py` (new test), `tests/integration/test_memory_service.py` (new test)

**Interfaces:**
- Consumes: `MCPApprovalPolicy(session_factory=, audit=, sensitivity_terms_provider=)` (Task 3), `extract_sensitivity_terms` (Task 2).
- Produces: `MCPManager.__init__(..., audit=None, sensitivity_terms_provider=None)`; patched `sdk_server._get_needs_approval_for_tool(tool, agent)` returning an async `(run_context, args, call_id) -> bool`; `MemoryService.sensitivity_terms() -> list[str]`.

- [ ] **Step 1: Write the failing manager test (append to tests/integration/test_mcp_manager.py, matching its existing imports/fixtures)**

```python
async def test_policy_guard_threads_arguments_into_needs_approval(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    try:
        async def terms():
            return ["mom@example.com"]

        policy = MCPApprovalPolicy(session_factory=factory, sensitivity_terms_provider=terms)
        cfg = MCPServerConfig(name="gmail", transport="stdio", command=["true"])
        sdk_server = _build_sdk_server(cfg, approval_policy=policy)

        tool = SimpleNamespace(name="create_draft", inputSchema={}, description="", annotations=None)
        needs_approval = sdk_server._get_needs_approval_for_tool(tool, agent=None)

        assert await needs_approval(None, {"to": "mom@example.com"}, "call-1") is True
        assert await needs_approval(None, {"to": "other@example.com"}, "call-2") is False
    finally:
        await engine.dispose()
```

Use whatever import style the file already has; add `from types import SimpleNamespace`, `from jarvis.mcp.manager import _build_sdk_server`, `from jarvis.mcp.approval_policy import MCPApprovalPolicy`, and the config class the file already imports (`MCPServerConfig` from `jarvis.config.schema`). If the file has an engine/factory fixture, reuse it instead of the inline engine setup.

- [ ] **Step 2: Write the failing memory test (append to tests/integration/test_memory_service.py, reusing its existing service/factory fixtures)**

```python
async def test_sensitivity_terms_come_from_active_preferences(...existing fixtures...):
    # Seed one approved preference with the marker and one without, via
    # MemoryPreferenceRepo (create_pending + approve/list_active pattern used
    # elsewhere in this file), then:
    terms = await service.sensitivity_terms()
    assert terms == ["mom@example.com", "salary"]
```

Follow the file's existing fixture names exactly; the assertion is the deliverable: only `sensitive:`-marked, *active* preferences contribute terms.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_mcp_manager.py tests/integration/test_memory_service.py -q`
Expected: new tests FAIL (arguments not threaded — needs_approval takes 2 positional args; `MemoryService` has no `sensitivity_terms`).

- [ ] **Step 4: Implement**

`jarvis/mcp/manager.py` — `MCPManager.__init__` gains and forwards the two params:

```python
    def __init__(
        self,
        *,
        config: MCPServersConfig,
        session_factory: async_sessionmaker[AsyncSession],
        secrets_key: bytes | None = None,
        oauth_flow=None,
        catalog: "ProviderCatalog | None" = None,
        connect_timeout: float = 60.0,
        close_timeout: float = 10.0,
        audit=None,
        sensitivity_terms_provider=None,
    ) -> None:
        ...
        self._approval_policy = MCPApprovalPolicy(
            session_factory=session_factory,
            audit=audit,
            sensitivity_terms_provider=sensitivity_terms_provider,
        )
```

`_apply_runtime_policy_guard` — after the existing `call_tool` patch, add:

```python
    def get_needs_approval_for_tool(tool, agent):
        # Replace the SDK's normalization (which drops call arguments) with a
        # per-call gate that sees them, so sensitivity escalation and the
        # policy-decision audit event have argument-level context. The
        # `require_approval` kwarg stays wired as a fallback should a future
        # SDK stop consulting this instance attribute.
        async def _needs_approval(run_context, args, call_id):
            return await approval_policy.needs_approval(
                name, tool, arguments=args, call_id=call_id
            )

        return _needs_approval

    sdk_server._get_needs_approval_for_tool = get_needs_approval_for_tool  # type: ignore[attr-defined]
```

`jarvis/main.py` — MCPManager construction becomes:

```python
    mcp_manager = MCPManager(
        config=cfg.mcp_servers,
        session_factory=factory,
        secrets_key=cfg.secrets_key,
        oauth_flow=oauth_flow,
        catalog=catalog,
        audit=audit,
        sensitivity_terms_provider=(
            memory_service.sensitivity_terms if memory_service is not None else None
        ),
    )
```

`jarvis/memory/service.py` — add import `from jarvis.mcp.sensitivity import extract_sensitivity_terms` and method:

```python
    async def sensitivity_terms(self) -> list[str]:
        """Known-sensitive terms (contacts/topics) from active preferences.

        Consumed by MCPApprovalPolicy so approval escalation is context-aware.
        Preferences opt in with a leading ``sensitive:`` marker.
        """
        contents, _error = await self._load_preferences()
        return extract_sensitivity_terms(contents)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_mcp_manager.py tests/integration/test_memory_service.py tests/integration/test_main_smoke.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/mcp/manager.py jarvis/main.py jarvis/memory/service.py tests/integration/test_mcp_manager.py tests/integration/test_memory_service.py
git commit -m "feat: thread call arguments, audit, and memory sensitivity terms into approval policy"
```

---

### Task 5: Full verification

**Files:** none new.

- [ ] **Step 1: Run the full check**

Run: `make check` (then `uv run pytest -q 2>&1 | tail -5` once if a summary is needed)
Expected: ruff clean, all tests pass.

- [ ] **Step 2: Fix anything that surfaced, re-run, commit fixes**

- [ ] **Step 3: Open PR**

Branch `approval-blast-radius`, push, `gh pr create` with a body summarizing: effect-based classification, sensitivity escalation from memory, `tool.policy_decision` audit coverage, unchanged non-user read-only restriction.

---

## Self-Review

- **Spec coverage:** blast-radius/reversibility classification → Task 1; memory-derived sensitivity → Tasks 2–4; audit of every auto-allowed action → Task 3 (decision events) + existing tracer TOOL_CALL events; unit tests for reversible-auto/destructive-gates → Task 1 Step 1; make check → Task 5. Gated volume drop: reversible mutations (create/update/label/archive/…) no longer gate — only irreversible/outward/unknown/sensitive do.
- **Placeholder scan:** Task 4 Steps 1–2 intentionally defer to existing fixture names in two test files (they must match the file's local conventions); all logic code is fully specified.
- **Type consistency:** `needs_approval(server_name, tool, arguments=None, call_id=None)` used identically in Tasks 3 and 4; `sensitivity_terms_provider` is a zero-arg async callable in Tasks 3 and 4; `ToolEffect` values (`"read"`, `"reversible"`, `"irreversible"`, `"unknown"`) match the audit payload assertions.

# Jarvis Plan 2 — Agent Core + MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the agent orchestration layer: a single `python -m jarvis invoke "prompt"` command that loads config, starts any configured MCP servers, runs an agent against a local OpenAI-compatible LLM, records every step to the audit log, and prints the final output. No Discord or scheduler yet — those come in Plans 3 and 4.

**Architecture:** `TriggerDispatcher` is the sole producer of `InvocationRequest`. `AgentRunner` owns the OpenAI Agents SDK run loop. `MCPManager` owns persistent MCP client connections and publishes them to the Agent as `mcp_servers`. `ToolPolicy` classifies tools (auto / confirm) from MCP annotations + name heuristics + DB overrides. A custom `Tracer` plugs into the SDK's tracing API and forwards span events to `AuditLogger` so every LLM call, tool call, and agent decision lands in the `audit_events` table.

**Tech Stack:** `openai-agents>=0.7`, `openai>=1.68` (the SDK's runtime dep), `mcp>=1.0` (bundled by openai-agents-python), `typer>=0.12` for the CLI (lightweight, familiar). Existing Plan 1 stack (SQLAlchemy async, Pydantic, watchfiles) remains unchanged.

**Design spec this plan implements:** `docs/superpowers/specs/2026-04-14-jarvis-agent-service-design.md` — sections covered: §5.1 AgentRunner, §5.2 TriggerDispatcher, §5.5 MCPManager, §5.6 ToolPolicy, §5.9 Tracer, partial §5.10 OutputRouter (just the manual/CLI path — channel routing is Plan 3), §6.1 Discord flow up to the LLM call (exercised via manual trigger in the smoke test), §6.3 convergence.

**Plan 1 follow-ups addressed here:** `MCPToolDescriptor` Pydantic type; composite index on `conversations(channel_kind, channel_ref, status)`; `MessageRole` enum; `ConversationRepo.touch` wiring; `idle_timeout_sec` column usage; `AuditLogger` bounded queue + drop counter; `AuditRepo.recent → AuditEvent` mapping.

---

## File Structure

New modules (paths relative to repo root):

```
jarvis/
  core/
    dispatcher.py       # TriggerDispatcher, InvocationRequest lifecycle
  mcp/
    descriptor.py       # MCPToolDescriptor (Pydantic contract for tool metadata)
    manager.py          # MCPManager: connection pool, tool catalog, reconnect
    tool_policy.py      # ToolPolicy.classify(tool) -> "auto" | "confirm"
  audit/
    tracer.py           # SDK tracing processor → AuditLogger bridge
  agents/
    runner.py           # AgentRunner: wraps OpenAI Agents SDK Runner
    llm_client.py       # build_llm_client(config): configures AsyncOpenAI
  cli.py                # typer app: "jarvis invoke", "jarvis check-config"
```

Files modified:
- `jarvis/core/types.py` — add `MessageRole` enum; add `AuditEvent.to_row()` / helpers as needed.
- `jarvis/persistence/models.py` — `idle_timeout_sec` column now nullable int, already present; no schema change here but ensure repo reads it.
- `jarvis/persistence/repositories.py` — add `ConversationRepo.get_idle_timeout`, wire `touch` into `MessageRepo.append`, add `AuditRepo.recent_as_events` (returns `list[AuditEvent]`).
- `jarvis/audit/logger.py` — add `maxsize` and `_dropped_count` to handle queue overflow.
- `jarvis/main.py` — `bootstrap()` returns a richer `AppContext` including `AgentRunner` + `MCPManager` + `TriggerDispatcher`.
- `pyproject.toml` — add `openai-agents`, `openai`, `typer` deps.
- `alembic/versions/0002_conv_composite_index.py` — new migration adding composite index.

---

## Task 1: Add Plan 2 dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit `pyproject.toml`**

In the `[project]` `dependencies` list, add these three entries (keeping existing entries):

```toml
dependencies = [
  "pydantic>=2.7",
  "sqlalchemy[asyncio]>=2.0.30",
  "aiosqlite>=0.20",
  "alembic>=1.13",
  "pyyaml>=6.0",
  "watchfiles>=0.22",
  "openai-agents>=0.7",
  "openai>=1.68",
  "typer>=0.12",
]
```

- [ ] **Step 2: Sync dependencies**

Run: `uv sync`

Expected: resolves and installs `openai-agents`, `openai`, `typer`, plus their transitive deps (the official `mcp` package is a transitive dep of `openai-agents`).

- [ ] **Step 3: Verify imports work**

Run:
```bash
uv run python -c "
from agents import Agent, Runner, RunConfig
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from agents import set_default_openai_client, set_tracing_disabled
from openai import AsyncOpenAI
import typer
print('all imports OK')
"
```

Expected: `all imports OK`. If `agents.mcp` import fails, the SDK version is too old — bump to `>=0.7` and re-sync.

- [ ] **Step 4: Run full suite to confirm nothing broke**

Run: `uv run pytest`
Expected: 56 passed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "add openai-agents, openai, and typer dependencies"
```

---

## Task 2: `MessageRole` enum

Plan 1 followup: `MessageRepo.append` currently accepts a free-form `str`. Tighten to a `StrEnum` so typos like `"User"` get caught at the boundary.

**Files:**
- Modify: `jarvis/core/types.py`
- Modify: `jarvis/persistence/repositories.py`
- Create: `tests/unit/test_message_role.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/test_message_role.py`:

```python
from jarvis.core.types import MessageRole


def test_message_role_values():
    assert MessageRole.USER.value == "user"
    assert MessageRole.ASSISTANT.value == "assistant"
    assert MessageRole.SYSTEM.value == "system"


def test_message_role_is_str_enum():
    # StrEnum members compare equal to their string value.
    assert MessageRole.USER == "user"
    assert "user" == MessageRole.USER
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/unit/test_message_role.py -v`
Expected: `ImportError` on `MessageRole`.

- [ ] **Step 3: Add `MessageRole` to `jarvis/core/types.py`**

Locate the block of `StrEnum` classes (near `ChannelKind`, `TriggerKind`, `AuditEventType`). Add after `AuditEventType`:

```python
class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
```

- [ ] **Step 4: Run new test — verify pass**

Run: `uv run pytest tests/unit/test_message_role.py -v`
Expected: 2 passed.

- [ ] **Step 5: Tighten `MessageRepo.append` signature**

In `jarvis/persistence/repositories.py`, find `MessageRepo.append`. Change the `role: str` parameter to `role: MessageRole`, and add `from jarvis.core.types import MessageRole` to the imports if not already present. When writing the row, use `role=role.value`.

The updated method (showing just the signature and body):

```python
    async def append(
        self,
        *,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
    ) -> MessageRow:
        msg = MessageRow(
            conversation_id=conversation_id,
            role=role.value,
            content=content,
            created_at=_utcnow(),
        )
        self._session.add(msg)
        await self._session.commit()
        await self._session.refresh(msg)
        return msg
```

- [ ] **Step 6: Update existing test to use the enum**

In `tests/integration/test_repositories_flow.py`, find `test_message_repo_appends`. Update the `role="user"` and `role="assistant"` arguments to `role=MessageRole.USER` and `role=MessageRole.ASSISTANT`. Add `from jarvis.core.types import MessageRole` to the imports.

- [ ] **Step 7: Full suite**

Run: `uv run pytest`
Expected: 58 passed (56 existing + 2 new).

- [ ] **Step 8: ruff**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: both clean.

- [ ] **Step 9: Commit**

```bash
git add jarvis/core/types.py jarvis/persistence/repositories.py tests/unit/test_message_role.py tests/integration/test_repositories_flow.py
git commit -m "add MessageRole enum and tighten MessageRepo.append signature"
```

---

## Task 3: Wire `ConversationRepo.touch` into `MessageRepo.append`

Plan 1 followup: `touch` exists but is uncalled. When a message lands on a conversation, we should bump the conversation's `last_activity_at` so the sliding-window idle timeout (from `find_or_create_open`) remains accurate even when messages arrive faster than we re-enter dispatch.

**Files:**
- Modify: `jarvis/persistence/repositories.py`
- Modify: `tests/integration/test_repositories_flow.py` (add one test)

- [ ] **Step 1: Write failing test**

Append to `tests/integration/test_repositories_flow.py`:

```python


async def test_message_append_touches_conversation(session):
    conv_repo = ConversationRepo(session)
    conv = await conv_repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    original_activity = conv.last_activity_at

    # Simulate the conversation aging.
    import asyncio
    await asyncio.sleep(0.02)

    msg_repo = MessageRepo(session)
    await msg_repo.append(
        conversation_id=conv.id,
        role=MessageRole.USER,
        content="hello",
    )

    await session.refresh(conv)
    assert conv.last_activity_at > original_activity
```

- [ ] **Step 2: Run — verify it currently fails**

Run: `uv run pytest tests/integration/test_repositories_flow.py::test_message_append_touches_conversation -v`
Expected: fails because `last_activity_at` is unchanged.

- [ ] **Step 3: Wire `touch` into `append`**

In `jarvis/persistence/repositories.py`, modify `MessageRepo.__init__` to optionally accept a `ConversationRepo`, and call `touch` at the end of `append`. Simplest approach: give `MessageRepo` a reference to its own `ConversationRepo`:

```python
class MessageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conv_repo = ConversationRepo(session)

    async def append(
        self,
        *,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
    ) -> MessageRow:
        msg = MessageRow(
            conversation_id=conversation_id,
            role=role.value,
            content=content,
            created_at=_utcnow(),
        )
        self._session.add(msg)
        await self._session.commit()
        await self._session.refresh(msg)
        await self._conv_repo.touch(conversation_id)
        return msg
```

Note: this creates a second `ConversationRepo` on the same session. That's fine — repos are stateless wrappers around a session, and the touch implementation does its own commit. No recursion risk.

- [ ] **Step 4: Run new test — verify pass**

Run: `uv run pytest tests/integration/test_repositories_flow.py::test_message_append_touches_conversation -v`
Expected: 1 passed.

- [ ] **Step 5: Full suite**

Run: `uv run pytest`
Expected: 59 passed.

- [ ] **Step 6: ruff**

Run: `uv run ruff check . && uv run ruff format --check .`

- [ ] **Step 7: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_flow.py
git commit -m "wire MessageRepo.append to touch conversation last_activity_at"
```

---

## Task 4: Composite index on `conversations(channel_kind, channel_ref, status)`

Plan 1 followup + design spec §7 performance note. The hot query in `ConversationRepo.find_or_create_open` filters on all three columns and orders by `last_activity_at` — a single composite index makes this an index-only seek.

**Files:**
- Modify: `jarvis/persistence/models.py` (add index declaration)
- Create: `alembic/versions/0002_conv_composite_index.py`
- Create: `tests/integration/test_conv_composite_index_migration.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_conv_composite_index_migration.py`:

```python
"""The composite index must exist after `alembic upgrade head` runs."""

import os
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [
            "uv", "run", "alembic",
            "-x", f"db_url=sqlite+aiosqlite:///{db_path}",
            *args,
        ],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ},
    )


def test_composite_index_exists_after_upgrade(tmp_path):
    db_path = tmp_path / "test.db"
    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    # Query sqlite_master directly for the composite index.
    import sqlite3
    c = sqlite3.connect(db_path)
    try:
        rows = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='conversations'"
        ).fetchall()
    finally:
        c.close()

    names = {r[0] for r in rows}
    assert any("channel_kind" in n and "channel_ref" in n for n in names), (
        f"composite index not found. indexes on conversations: {names}"
    )
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_conv_composite_index_migration.py -v`
Expected: fails — no composite index exists yet.

- [ ] **Step 3: Add the composite index to the ORM model**

In `jarvis/persistence/models.py`, add this import at the top alongside the other sqlalchemy imports:

```python
from sqlalchemy import Index
```

Then add `__table_args__` to `ConversationRow` (directly after the fields, before the `messages` relationship):

```python
    __table_args__ = (
        Index(
            "ix_conversations_lookup",
            "channel_kind",
            "channel_ref",
            "status",
        ),
    )
```

- [ ] **Step 4: Auto-generate the migration**

Run:
```bash
cd /Users/mdolton/dev/jarvis
uv run alembic revision --autogenerate -m "composite index for conversation lookup"
```

- [ ] **Step 5: Rename the migration for stable ordering**

Find the generated file (will have a random revision prefix) and rename:

```bash
cd /Users/mdolton/dev/jarvis
ls alembic/versions/*composite_index*.py  # confirm what got created
mv alembic/versions/*composite_index*.py alembic/versions/0002_conv_composite_index.py
```

Open the renamed file and change the `revision` and `down_revision` lines to:

```python
revision: str = "0002"
down_revision: str | None = "0001"
```

(The autogenerator should have set `down_revision` correctly, but pin it explicitly.)

- [ ] **Step 6: Inspect the `upgrade()` function**

Open `alembic/versions/0002_conv_composite_index.py`. The `upgrade()` body should contain a single `op.create_index(...)` call for `ix_conversations_lookup`. If it contains anything else (e.g., unrelated column changes), delete those — something drifted and you need to report it rather than commit a polluted migration. The `downgrade()` should contain the matching `op.drop_index(...)`.

Expected `upgrade()`:
```python
def upgrade() -> None:
    op.create_index(
        "ix_conversations_lookup",
        "conversations",
        ["channel_kind", "channel_ref", "status"],
        unique=False,
    )
```

Expected `downgrade()`:
```python
def downgrade() -> None:
    op.drop_index("ix_conversations_lookup", table_name="conversations")
```

Clean up the file's imports and empty `op` calls if autogeneration left noise.

- [ ] **Step 7: Run the migration test — verify pass**

Run: `uv run pytest tests/integration/test_conv_composite_index_migration.py -v`
Expected: 1 passed.

- [ ] **Step 8: Run both migration round-trips to confirm no regression**

Run: `uv run pytest tests/integration/test_migrations.py -v`
Expected: 2 passed (existing 0001 tests still green — upgrade now applies 0001 AND 0002).

- [ ] **Step 9: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 60 passed, ruff clean.

- [ ] **Step 10: Commit**

```bash
git add jarvis/persistence/models.py alembic/versions/0002_conv_composite_index.py tests/integration/test_conv_composite_index_migration.py
git commit -m "add composite index on conversations(channel_kind, channel_ref, status)"
```

---

## Task 5: `MCPToolDescriptor` Pydantic type

Plan 1 followup. Define the typed contract that `MCPManager` will produce and `MCPToolRepo.replace_for_server` will consume.

**Files:**
- Create: `jarvis/mcp/descriptor.py`
- Create: `tests/unit/test_mcp_descriptor.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/test_mcp_descriptor.py`:

```python
import pytest
from pydantic import ValidationError

from jarvis.mcp.descriptor import MCPToolDescriptor


def test_mcp_tool_descriptor_minimal():
    t = MCPToolDescriptor(name="list_events", input_schema={"type": "object"})
    assert t.name == "list_events"
    assert t.description == ""
    assert t.read_only_hint is None
    assert t.destructive_hint is None


def test_mcp_tool_descriptor_full():
    t = MCPToolDescriptor(
        name="send_email",
        description="Send an email",
        input_schema={"type": "object", "properties": {"to": {"type": "string"}}},
        read_only_hint=False,
        destructive_hint=False,
    )
    assert t.description == "Send an email"
    assert t.read_only_hint is False


def test_mcp_tool_descriptor_rejects_extra_fields():
    with pytest.raises(ValidationError):
        MCPToolDescriptor(
            name="x",
            input_schema={},
            policy_override="confirm",  # not a field — confirm flow is repo-managed
        )  # type: ignore[call-arg]


def test_mcp_tool_descriptor_requires_input_schema():
    with pytest.raises(ValidationError):
        MCPToolDescriptor(name="x")  # type: ignore[call-arg]
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/unit/test_mcp_descriptor.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/mcp/descriptor.py`**

```python
"""MCPToolDescriptor — typed contract for MCP tool metadata.

Produced by MCPManager when it enumerates tools from an MCP server.
Consumed by MCPToolRepo.replace_for_server when shadowing the catalog
to SQLite for the dashboard.
"""

from pydantic import BaseModel, ConfigDict


class MCPToolDescriptor(BaseModel):
    """Metadata for a single MCP tool, independent of its runtime invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    input_schema: dict
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
```

- [ ] **Step 4: Update `MCPToolRepo.replace_for_server` to accept descriptors**

In `jarvis/persistence/repositories.py`, update the method signature and body:

```python
    async def replace_for_server(
        self,
        server_id: UUID,
        *,
        tools: list[MCPToolDescriptor],
    ) -> None:
        """Replace the tool set for a server atomically (full overwrite)."""
        existing = await self._session.execute(
            select(MCPToolRow).where(MCPToolRow.server_id == server_id)
        )
        for row in existing.scalars():
            await self._session.delete(row)

        for tool in tools:
            self._session.add(
                MCPToolRow(
                    server_id=server_id,
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    read_only_hint=tool.read_only_hint,
                    destructive_hint=tool.destructive_hint,
                    policy_override=None,  # not part of the descriptor contract
                )
            )
        await self._session.commit()
```

Add the import at the top: `from jarvis.mcp.descriptor import MCPToolDescriptor`.

- [ ] **Step 5: Update the existing repo test**

In `tests/integration/test_repositories_mcp_settings.py`, find `test_mcp_tool_replace_for_server`. The test passes `list[dict]` — replace with `list[MCPToolDescriptor]`. Add `from jarvis.mcp.descriptor import MCPToolDescriptor` at the top. Rewrite the tools list:

```python
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
```

And the second `replace_for_server` call becomes:

```python
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
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/test_mcp_descriptor.py tests/integration/test_repositories_mcp_settings.py -v`
Expected: 8 passed (4 new + 4 updated existing).

- [ ] **Step 7: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 64 passed, ruff clean.

- [ ] **Step 8: Commit**

```bash
git add jarvis/mcp/descriptor.py jarvis/persistence/repositories.py tests/unit/test_mcp_descriptor.py tests/integration/test_repositories_mcp_settings.py
git commit -m "add MCPToolDescriptor type and tighten MCPToolRepo signature"
```

---

## Task 6: `ToolPolicy` — classify MCP tools

Design spec §5.6. Pure function that decides whether a tool should auto-execute or require confirmation. Plan 2 is autonomous-only (all tools execute), but the classification is recorded on every `tool.call` audit event so Plan 2+ can show "what would this look like with confirmation turned on" in the dashboard.

**Files:**
- Create: `jarvis/mcp/tool_policy.py`
- Create: `tests/unit/test_tool_policy.py`

- [ ] **Step 1: Write failing tests**

Write `tests/unit/test_tool_policy.py`:

```python
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.tool_policy import ToolPolicy, classify


def _desc(**kwargs) -> MCPToolDescriptor:
    defaults = {"name": "x", "input_schema": {}}
    defaults.update(kwargs)
    return MCPToolDescriptor(**defaults)


def test_user_override_wins():
    t = _desc(name="whatever", destructive_hint=True)
    assert classify(t, override="auto") == ToolPolicy.AUTO
    assert classify(t, override="confirm") == ToolPolicy.CONFIRM


def test_read_only_hint_auto():
    assert classify(_desc(name="fetch", read_only_hint=True)) == ToolPolicy.AUTO


def test_destructive_hint_confirm():
    assert (
        classify(_desc(name="list_events", destructive_hint=True))
        == ToolPolicy.CONFIRM
    )


def test_destructive_wins_over_read_only():
    """If a tool is both read_only AND destructive, destructive wins."""
    t = _desc(name="x", read_only_hint=True, destructive_hint=True)
    assert classify(t) == ToolPolicy.CONFIRM


def test_heuristic_read_prefixes():
    for name in ("get_thing", "list_things", "read_item", "search_docs", "fetch_url"):
        assert classify(_desc(name=name)) == ToolPolicy.AUTO, name


def test_heuristic_unknown_defaults_to_confirm():
    for name in ("send_email", "delete_event", "execute_query", "do_thing"):
        assert classify(_desc(name=name)) == ToolPolicy.CONFIRM, name


def test_heuristic_case_insensitive():
    assert classify(_desc(name="GET_Thing")) == ToolPolicy.AUTO
    assert classify(_desc(name="List_Things")) == ToolPolicy.AUTO


def test_override_accepts_none():
    """override=None falls through to annotation/heuristic."""
    assert classify(_desc(name="get_x"), override=None) == ToolPolicy.AUTO
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/unit/test_tool_policy.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/mcp/tool_policy.py`**

```python
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
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/unit/test_tool_policy.py -v`
Expected: 8 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 72 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/mcp/tool_policy.py tests/unit/test_tool_policy.py
git commit -m "add ToolPolicy classifier for MCP tool auto/confirm decision"
```

---

## Task 7: `AuditLogger` bounded queue + drop counter

Plan 1 followup: the unbounded queue could grow without limit if the DB stalls. Add a `maxsize` with drop-oldest semantics, and expose a drop counter so the dashboard can surface it later.

**Files:**
- Modify: `jarvis/audit/logger.py`
- Modify: `tests/integration/test_audit_logger.py` (add tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/integration/test_audit_logger.py`:

```python


async def test_queue_maxsize_drops_oldest(engine_and_factory):
    """When the queue is full on emit, drop the OLDEST buffered event."""
    _, factory = engine_and_factory
    # Very slow flush + tiny queue so we can force overflow deterministically.
    logger = AuditLogger(
        session_factory=factory,
        flush_interval_sec=5.0,  # effectively never auto-flush during the test
        batch_size=100,
        max_queue_size=3,
    )
    await logger.start()
    try:
        # Produce more events than the queue holds; they can't flush yet.
        for i in range(6):
            await logger.emit(
                AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"i": i})
            )
        # 6 emits into a 3-slot queue: 3 dropped.
        assert logger.dropped_count == 3
    finally:
        await logger.stop()

    async with factory() as s:
        rows = await AuditRepo(s).recent(limit=10)
    # The 3 newest (i=3, 4, 5) survived; the 3 oldest (i=0, 1, 2) dropped.
    ids = sorted(r.payload["i"] for r in rows)
    assert ids == [3, 4, 5]


async def test_dropped_count_starts_at_zero(engine_and_factory):
    _, factory = engine_and_factory
    logger = AuditLogger(session_factory=factory)
    assert logger.dropped_count == 0
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_audit_logger.py -v`
Expected: failures on the two new tests (no `max_queue_size`, no `dropped_count`).

- [ ] **Step 3: Update `AuditLogger` to support max queue + drop counter**

In `jarvis/audit/logger.py`, add a `max_queue_size` constructor parameter and a `dropped_count` attribute. Modify `emit()` so that if the queue is full, it drops the OLDEST event to make room (which naturally prioritizes more recent context).

```python
class AuditLogger:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        flush_interval_sec: float = 0.1,
        batch_size: int = 50,
        max_queue_size: int = 10_000,
    ) -> None:
        self._session_factory = session_factory
        self._flush_interval = flush_interval_sec
        self._batch_size = batch_size
        self._max_queue_size = max_queue_size
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.dropped_count: int = 0
```

And `emit`:

```python
    async def emit(self, event: AuditEvent) -> None:
        if self._task is None:
            raise RuntimeError("AuditLogger not started")
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event to make room. The flusher never removes
            # from the head AND from the tail simultaneously, so this is
            # safe in a single-consumer design.
            try:
                self._queue.get_nowait()
                self.dropped_count += 1
            except asyncio.QueueEmpty:
                # Race: flusher just drained it. Still drop this event
                # to respect backpressure contract.
                self.dropped_count += 1
                return
            # Retry put — guaranteed to succeed now.
            self._queue.put_nowait(event)
```

Note: we removed the `await self._queue.put(event)` blocking path in favor of `put_nowait` — emits should never block the caller. Under normal load (queue not full) this is identical; under overload, we drop-oldest and increment the counter.

- [ ] **Step 4: Run new tests — verify pass**

Run: `uv run pytest tests/integration/test_audit_logger.py -v`
Expected: 6 passed (4 existing + 2 new).

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 74 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/audit/logger.py tests/integration/test_audit_logger.py
git commit -m "add bounded queue and dropped_count to AuditLogger"
```

---

## Task 8: `AuditRepo.recent_as_events`

Plan 1 followup: `AuditRepo.recent` returns ORM rows, but downstream (dashboard, CLI pretty-printer) want `AuditEvent` Pydantic models. Add a parallel method that returns the domain type. Keep `recent()` for internal use; export the Pydantic version for external callers.

**Files:**
- Modify: `jarvis/persistence/repositories.py`
- Modify: `tests/integration/test_repositories_audit_schedule.py` (add one test)

- [ ] **Step 1: Write failing test**

Append to `tests/integration/test_repositories_audit_schedule.py`:

```python


async def test_audit_recent_as_events_returns_pydantic(session):
    repo = AuditRepo(session)
    await repo.write_many([
        AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"x": 1}),
        AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"y": 2}),
    ])
    events = await repo.recent_as_events(limit=10)
    assert len(events) == 2
    # Returned as AuditEvent Pydantic instances with typed enums.
    assert all(isinstance(e, AuditEvent) for e in events)
    assert all(isinstance(e.type, AuditEventType) for e in events)
    # Newest first ordering preserved.
    assert events[0].created_at >= events[-1].created_at
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py::test_audit_recent_as_events_returns_pydantic -v`
Expected: fails — no `recent_as_events` method.

- [ ] **Step 3: Add the mapping method**

In `jarvis/persistence/repositories.py`, add this to `AuditRepo`:

```python
    async def recent_as_events(
        self,
        *,
        types: list[AuditEventType] | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Same as recent(), but maps each row to an AuditEvent Pydantic model."""
        rows = await self.recent(types=types, limit=limit)
        return [
            AuditEvent(
                id=r.id,
                conversation_id=r.conversation_id,
                trigger_id=r.trigger_id,
                type=AuditEventType(r.type),
                payload=r.payload,
                created_at=r.created_at,
            )
            for r in rows
        ]
```

- [ ] **Step 4: Run test — verify pass**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py::test_audit_recent_as_events_returns_pydantic -v`
Expected: 1 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 75 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_audit_schedule.py
git commit -m "add AuditRepo.recent_as_events returning Pydantic AuditEvent"
```

---

## Task 9: `LLMClient` builder

Thin factory: given a `LLMConfig`, return an `AsyncOpenAI` client pointing at the configured base_url, and install it as the default so the Agents SDK uses it. Also disables the OpenAI trace upload (the SDK would try to POST to `platform.openai.com` on every run otherwise).

**Files:**
- Create: `jarvis/agents/__init__.py`
- Create: `jarvis/agents/llm_client.py`
- Create: `tests/unit/test_llm_client.py`

- [ ] **Step 1: Create the agents subpackage**

Run: `touch /Users/mdolton/dev/jarvis/jarvis/agents/__init__.py`

- [ ] **Step 2: Write failing test**

Write `tests/unit/test_llm_client.py`:

```python
from openai import AsyncOpenAI

from jarvis.agents.llm_client import build_llm_client
from jarvis.config.schema import LLMConfig


def test_build_llm_client_uses_configured_base_url():
    cfg = LLMConfig(
        base_url="http://host.docker.internal:1234/v1",
        api_key="dummy",
        model="qwen2.5:32b",
    )
    client = build_llm_client(cfg)
    assert isinstance(client, AsyncOpenAI)
    assert str(client.base_url) == "http://host.docker.internal:1234/v1/"


def test_build_llm_client_request_timeout_applied():
    cfg = LLMConfig(
        base_url="http://x/v1",
        api_key="k",
        model="m",
        request_timeout_sec=30.0,
    )
    client = build_llm_client(cfg)
    # httpx Client carries the timeout; the OpenAI SDK exposes it as .timeout.
    assert client.timeout.read == 30.0
```

- [ ] **Step 3: Run — verify failure**

Run: `uv run pytest tests/unit/test_llm_client.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Write `jarvis/agents/llm_client.py`**

```python
"""LLM client builder.

Wraps the `openai.AsyncOpenAI` constructor with config-driven values and
installs the client as the Agents SDK default so every Runner.run() call
uses the configured endpoint without per-call plumbing.
"""

from agents import set_default_openai_client, set_tracing_disabled
from openai import AsyncOpenAI

from jarvis.config.schema import LLMConfig


def build_llm_client(cfg: LLMConfig) -> AsyncOpenAI:
    """Build an AsyncOpenAI client pointed at the configured endpoint.

    Does NOT install it globally — use `install_as_default` for that. This
    split lets tests build a client without clobbering the process-wide
    Agents SDK default.
    """
    return AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout=cfg.request_timeout_sec,
    )


def install_as_default(client: AsyncOpenAI) -> None:
    """Install `client` as the Agents SDK's default OpenAI client.

    Also disables the default OpenAI tracing exporter, which would try to
    POST trace spans to platform.openai.com and fail with 401 against a
    local LLM endpoint. Our custom tracer (Task 11) still receives events.
    """
    set_default_openai_client(client)
    set_tracing_disabled(True)
```

Two functions — build + install — so tests can instantiate without side effects.

- [ ] **Step 5: Run tests — verify pass**

Run: `uv run pytest tests/unit/test_llm_client.py -v`
Expected: 2 passed.

- [ ] **Step 6: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 77 passed, clean.

- [ ] **Step 7: Commit**

```bash
git add jarvis/agents/__init__.py jarvis/agents/llm_client.py tests/unit/test_llm_client.py
git commit -m "add LLM client builder for OpenAI-compatible endpoints"
```

---

## Task 10: `MCPManager`

Owns the lifecycle of all configured MCP servers. Reads the config list, establishes (and maintains) connections, exposes the Agents-SDK-compatible `mcp_servers` list, and shadows the tool catalog + server status to the DB.

v1 simplifications:
- Sequential connect on startup (not parallel) — simpler and fast enough for handful-of-servers configs.
- Background reconnect loop with exponential backoff (max 60s).
- No tool-level caching logic of our own — the Agents SDK handles that via `cache_tools_list=True` on the `MCPServer*` classes.

**Files:**
- Create: `jarvis/mcp/manager.py`
- Create: `tests/integration/test_mcp_manager.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_mcp_manager.py`:

```python
"""MCPManager integration tests using a real in-process stdio MCP server."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.manager import MCPManager
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo


# --- A minimal stdio MCP server, written as a standalone script ---
# We use the official `mcp` SDK (a transitive dep of openai-agents) to spin
# up a one-tool server that we then ask MCPManager to connect to.
_SERVER_SCRIPT = '''
import asyncio
import sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("test-server")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo the input back",
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    return [TextContent(type="text", text=arguments.get("text", ""))]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

asyncio.run(main())
'''


@pytest.fixture
async def engine_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield engine, factory
    await engine.dispose()


@pytest.fixture
def test_server_script(tmp_path):
    path = tmp_path / "mcp_server.py"
    path.write_text(_SERVER_SCRIPT)
    return path


async def test_mcp_manager_connects_and_catalogs_tools(
    engine_and_factory, test_server_script
):
    _, factory = engine_and_factory

    cfg = MCPServersConfig(
        servers=[
            MCPServerConfig(
                name="test",
                transport="stdio",
                command=[sys.executable, str(test_server_script)],
            ),
        ],
    )

    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    try:
        # Manager exposes the SDK server list for the Agent.
        sdk_servers = manager.agent_mcp_servers()
        assert len(sdk_servers) == 1

        # DB shadow: one server marked connected, one tool recorded.
        async with factory() as s:
            servers = await MCPServerRepo(s).list_all()
            assert len(servers) == 1
            assert servers[0].name == "test"
            assert servers[0].status == "connected"

            tools = await MCPToolRepo(s).list_for_server(servers[0].id)
            assert {t.name for t in tools} == {"echo"}
    finally:
        await manager.stop()


async def test_mcp_manager_records_failure_for_bad_command(engine_and_factory):
    _, factory = engine_and_factory

    cfg = MCPServersConfig(
        servers=[
            MCPServerConfig(
                name="broken",
                transport="stdio",
                command=["/nonexistent/binary"],
            ),
        ],
    )

    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    try:
        async with factory() as s:
            servers = await MCPServerRepo(s).list_all()
            assert len(servers) == 1
            assert servers[0].status in ("disconnected", "error")
            assert servers[0].last_error is not None
    finally:
        await manager.stop()


async def test_mcp_manager_handles_empty_config(engine_and_factory):
    _, factory = engine_and_factory
    cfg = MCPServersConfig(servers=[])
    manager = MCPManager(config=cfg, session_factory=factory)
    await manager.start()
    try:
        assert manager.agent_mcp_servers() == []
    finally:
        await manager.stop()
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_mcp_manager.py -v`
Expected: `ModuleNotFoundError` on `jarvis.mcp.manager`.

- [ ] **Step 3: Write `jarvis/mcp/manager.py`**

```python
"""MCPManager — owns lifecycle of all configured MCP servers.

- On start(): spin up each enabled MCP server (stdio, http, or sse),
  record status + discovered tools to the DB shadow tables.
- While running: the Agents SDK owns the actual connections and tool
  caching via `cache_tools_list=True`. We just keep the SDK server
  objects alive for the Agent to use.
- On stop(): async-close each SDK server cleanly.
"""

import logging
from contextlib import AsyncExitStack

from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo

_log = logging.getLogger(__name__)


class MCPManager:
    def __init__(
        self,
        *,
        config: MCPServersConfig,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._stack = AsyncExitStack()
        self._sdk_servers: list[object] = []  # opaque agents.mcp.MCPServer*

    async def start(self) -> None:
        """Connect to every enabled server. Failures are recorded, not raised."""
        for server_cfg in self._config.servers:
            if not server_cfg.enabled:
                continue
            try:
                await self._connect_one(server_cfg)
            except Exception as e:  # noqa: BLE001 — one bad server mustn't kill the rest
                _log.exception("failed to connect MCP server %r", server_cfg.name)
                await self._record_failure(server_cfg, e)

    async def stop(self) -> None:
        await self._stack.aclose()
        self._sdk_servers = []

    def agent_mcp_servers(self) -> list[object]:
        """Return the SDK server objects to pass into `Agent(mcp_servers=...)`."""
        return list(self._sdk_servers)

    async def _connect_one(self, cfg: MCPServerConfig) -> None:
        # Persist (or upsert) the server row up front so even a connection
        # failure later has something to attach the error to.
        async with self._session_factory() as session:
            row = await MCPServerRepo(session).upsert(
                name=cfg.name, transport=cfg.transport
            )
            server_id = row.id

        sdk_server = _build_sdk_server(cfg)
        await self._stack.enter_async_context(sdk_server)
        self._sdk_servers.append(sdk_server)

        # Enumerate tools — this confirms the connection actually works.
        tools = await _list_tools(sdk_server)

        async with self._session_factory() as session:
            srepo = MCPServerRepo(session)
            trepo = MCPToolRepo(session)
            await srepo.set_status(server_id, status="connected", last_error=None)
            await trepo.replace_for_server(server_id, tools=tools)

    async def _record_failure(self, cfg: MCPServerConfig, exc: Exception) -> None:
        async with self._session_factory() as session:
            repo = MCPServerRepo(session)
            row = await repo.upsert(name=cfg.name, transport=cfg.transport)
            await repo.set_status(
                row.id, status="error", last_error=f"{type(exc).__name__}: {exc}"
            )


def _build_sdk_server(cfg: MCPServerConfig) -> object:
    """Instantiate the right `agents.mcp` server class for `cfg`."""
    if cfg.transport == "stdio":
        return MCPServerStdio(
            name=cfg.name,
            params={
                "command": cfg.command[0],
                "args": cfg.command[1:],
                "env": cfg.env or None,
            },
            cache_tools_list=True,
        )
    if cfg.transport == "http":
        return MCPServerStreamableHttp(
            name=cfg.name,
            params={"url": cfg.url, "headers": cfg.headers or {}},
            cache_tools_list=True,
        )
    if cfg.transport == "sse":
        return MCPServerSse(
            name=cfg.name,
            params={"url": cfg.url, "headers": cfg.headers or {}},
            cache_tools_list=True,
        )
    raise ValueError(f"unsupported transport: {cfg.transport}")


async def _list_tools(sdk_server: object) -> list[MCPToolDescriptor]:
    """Ask the SDK server for its tools and map to our typed descriptors.

    The Agents SDK exposes `await server.list_tools()` which returns an MCP
    `ListToolsResult` or a list of `mcp.types.Tool` — both shapes have `.name`,
    `.description`, `.inputSchema`, and optionally `.annotations`.
    """
    raw_tools = await sdk_server.list_tools()  # type: ignore[attr-defined]
    descriptors: list[MCPToolDescriptor] = []
    for t in raw_tools:
        ann = getattr(t, "annotations", None)
        descriptors.append(
            MCPToolDescriptor(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.inputSchema) if t.inputSchema else {},
                read_only_hint=getattr(ann, "readOnlyHint", None) if ann else None,
                destructive_hint=getattr(ann, "destructiveHint", None) if ann else None,
            )
        )
    return descriptors
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_mcp_manager.py -v`
Expected: 3 passed. These tests may take 1-3 seconds (they spawn real subprocess MCP servers).

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 80 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/mcp/manager.py tests/integration/test_mcp_manager.py
git commit -m "add MCPManager: lifecycle, catalog shadow, multi-transport"
```

---

## Task 11: `Tracer` — SDK tracing → AuditLogger bridge

Design spec §5.9. The Agents SDK emits trace spans for every agent decision, LLM call, tool call, and tool result. We install a custom trace processor that converts each span into an `AuditEvent` and pushes it to our `AuditLogger`.

**Files:**
- Create: `jarvis/audit/tracer.py`
- Create: `tests/integration/test_tracer.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_tracer.py`:

```python
"""Tracer integration test: runs a real Agents SDK Runner with a scripted
mock LLM and asserts that the expected audit events land in the DB.
"""

import pytest
from agents import Agent, Runner, RunConfig, set_trace_processors

from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.core.types import AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo


@pytest.fixture
async def engine_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield engine, factory
    await engine.dispose()


async def test_tracer_emits_audit_events_for_agent_run(engine_and_factory):
    """A minimal Agent run with no tools should still produce at least one
    llm.request / llm.response pair of audit events.
    """
    from openai import AsyncOpenAI
    from agents import set_default_openai_client

    _, factory = engine_and_factory

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    try:
        # Install our tracer as the sole processor (this also silences the
        # OpenAI-backend exporter that would 401 against our fake client).
        tracer = JarvisTraceProcessor(audit)
        set_trace_processors([tracer])

        # Point the SDK at a dummy OpenAI-compatible endpoint. We won't
        # actually run the LLM — we instead use the `FakeModelRecorder`
        # below to capture that the SDK tried to call it.
        class _FakeModel:
            async def get_response(self, *a, **kw):
                # Minimal response shape the SDK expects.
                from agents.items import ModelResponse, Usage
                return ModelResponse(
                    output=[],  # no tool calls, no content
                    usage=Usage(),
                    response_id=None,
                )

        agent = Agent(name="t", instructions="x", model=_FakeModel())
        # Run — even a trivial run produces a trace span.
        await Runner.run(agent, "hi", run_config=RunConfig(workflow_name="test"))

        # Let the tracer's emits flush through AuditLogger.
        import asyncio
        await asyncio.sleep(0.1)
    finally:
        await audit.stop()

    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    # We expect at least one event of some LLM-related kind.
    types = {e.type for e in events}
    assert types & {
        AuditEventType.LLM_REQUEST.value,
        AuditEventType.LLM_RESPONSE.value,
    }, f"no LLM audit events seen; got types {types}"
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_tracer.py -v`
Expected: `ModuleNotFoundError` on `jarvis.audit.tracer`.

- [ ] **Step 3: Write `jarvis/audit/tracer.py`**

```python
"""Agents SDK trace processor that forwards spans to our AuditLogger.

The SDK emits typed spans for: agent start/end, LLM generations (request +
response), tool calls, and tool results. Each span becomes one AuditEvent
with the span's structured data as payload.

We install this via `set_trace_processors([JarvisTraceProcessor(...)])` in
the bootstrap, which replaces the default OpenAI-backend exporter. When
paired with `set_tracing_disabled(True)` in llm_client.install_as_default,
no traces leak to OpenAI.
"""

import asyncio
import logging
from typing import Any

from agents.tracing import Span, Trace, TracingProcessor

from jarvis.audit.logger import AuditLogger
from jarvis.core.types import AuditEvent, AuditEventType

_log = logging.getLogger(__name__)

# Map Agents SDK span types to our audit event types. The SDK's span data
# classes live in `agents.tracing.span_data` — we match by class name string
# to avoid importing every symbol.
_SPAN_TYPE_TO_AUDIT: dict[str, AuditEventType] = {
    "GenerationSpanData": AuditEventType.LLM_RESPONSE,
    "ResponseSpanData": AuditEventType.LLM_RESPONSE,
    "FunctionSpanData": AuditEventType.TOOL_CALL,
    "MCPListToolsSpanData": AuditEventType.MCP_CONNECTED,
}


class JarvisTraceProcessor(TracingProcessor):
    """Forwards every SDK span to AuditLogger as an AuditEvent."""

    def __init__(self, logger: AuditLogger) -> None:
        self._logger = logger

    def on_trace_start(self, trace: Trace) -> None:
        # Trace-level events aren't in our AuditEventType enum; we emit them
        # as LLM_REQUEST at trace start to mark the invocation boundary.
        self._emit(
            AuditEventType.LLM_REQUEST,
            payload={
                "trace_id": getattr(trace, "trace_id", None),
                "workflow_name": getattr(trace, "name", None),
                "phase": "start",
            },
        )

    def on_trace_end(self, trace: Trace) -> None:
        return

    def on_span_start(self, span: Span[Any]) -> None:
        return

    def on_span_end(self, span: Span[Any]) -> None:
        span_type_name = type(span.span_data).__name__
        audit_type = _SPAN_TYPE_TO_AUDIT.get(span_type_name)
        if audit_type is None:
            return
        payload: dict[str, Any] = {
            "span_type": span_type_name,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
        }
        # Best-effort: serialize span_data's public attributes.
        data = span.span_data
        for attr in dir(data):
            if attr.startswith("_"):
                continue
            val = getattr(data, attr, None)
            if callable(val):
                continue
            try:
                _json_safe(val)
            except (TypeError, ValueError):
                continue
            payload[attr] = val
        self._emit(audit_type, payload=payload)

    def shutdown(self) -> None:
        return

    def force_flush(self) -> None:
        return

    def _emit(self, audit_type: AuditEventType, *, payload: dict) -> None:
        event = AuditEvent(type=audit_type, payload=_json_safe_dict(payload))
        # `on_*` are sync callbacks; schedule the async emit.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("no running loop; dropping trace event %s", audit_type)
            return
        loop.create_task(self._logger.emit(event))


def _json_safe(value: Any) -> None:
    """Raise TypeError/ValueError if `value` is not JSON-serializable."""
    import json

    json.dumps(value, default=str)


def _json_safe_dict(d: dict) -> dict:
    import json

    return json.loads(json.dumps(d, default=str))
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_tracer.py -v`
Expected: 1 passed. If it fails because the SDK's span class names have changed, inspect `span_data.__class__.__name__` in a quick debug run and update the `_SPAN_TYPE_TO_AUDIT` mapping — this dict is the contract with the SDK.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 81 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/audit/tracer.py tests/integration/test_tracer.py
git commit -m "add JarvisTraceProcessor bridging Agents SDK traces to AuditLogger"
```

---

## Task 12: `AgentRunner`

The core agent execution primitive. Given an `InvocationRequest`, loads conversation history, hands off to the Agents SDK `Runner`, persists the assistant response, and returns the final text. The SDK's tracing is already wired to `AuditLogger` via Task 11.

**Files:**
- Create: `jarvis/agents/runner.py`
- Create: `tests/integration/test_agent_runner.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_agent_runner.py`:

```python
"""AgentRunner integration tests using a fake model — we assert on the
event stream / DB effects, not on LLM output content.
"""

import pytest
from agents import set_trace_processors
from agents.items import ModelResponse, Usage

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.types import (
    AuditEventType,
    ChannelKind,
    InvocationRequest,
    ManualTrigger,
    MessageRole,
)
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    AuditRepo,
    ConversationRepo,
    MessageRepo,
)


class _FakeModel:
    """Return a single text response with no tool calls."""

    def __init__(self, text: str = "hello from the fake") -> None:
        self._text = text

    async def get_response(self, *a, **kw):
        # The SDK expects output to contain message items. Minimal shape:
        from agents.items import MessageOutputItem  # type: ignore[attr-defined]

        return ModelResponse(
            output=[MessageOutputItem(text=self._text)],  # type: ignore[call-arg]
            usage=Usage(),
            response_id=None,
        )


@pytest.fixture
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    set_trace_processors([JarvisTraceProcessor(audit)])

    yield engine, factory, audit

    await audit.stop()
    await engine.dispose()


async def test_agent_runner_persists_user_and_assistant_messages(infra):
    _, factory, audit = infra
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(text="response-under-test"),
    )

    req = InvocationRequest(
        trigger=ManualTrigger(user="mark", prompt="hello agent"),
    )
    result = await runner.run(req)

    assert "response-under-test" in result.final_output

    # DB state
    async with factory() as s:
        conv_repo = ConversationRepo(s)
        # Manual triggers should create a DASHBOARD conversation.
        # (We don't have a direct accessor by trigger, so we just list
        # all conversations — there should be exactly one.)
        from sqlalchemy import select
        from jarvis.persistence.models import ConversationRow

        rows = (await s.execute(select(ConversationRow))).scalars().all()
        assert len(rows) == 1
        conv = rows[0]
        assert conv.channel_kind == ChannelKind.DASHBOARD.value

        msgs = await MessageRepo(s).history(conv.id)
        assert [m.role for m in msgs] == [
            MessageRole.USER.value,
            MessageRole.ASSISTANT.value,
        ]
        assert msgs[1].content == "response-under-test"


async def test_agent_runner_writes_audit_events(infra):
    _, factory, audit = infra
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
    )
    req = InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hi"))
    await runner.run(req)

    # Let the audit logger drain.
    import asyncio
    await asyncio.sleep(0.1)

    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    types = {e.type for e in events}
    # At minimum: trigger.received is emitted directly by AgentRunner.
    assert AuditEventType.TRIGGER_RECEIVED.value in types
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_agent_runner.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/agents/runner.py`**

```python
"""AgentRunner — wraps OpenAI Agents SDK Runner with our persistence.

Responsibilities:
  1. Convert an `InvocationRequest` into a conversation + user message row.
  2. Build system prompt + hand off to `agents.Runner.run`.
  3. Persist assistant output as a message row.
  4. Emit audit events at trigger boundaries (the SDK tracing handles the
     intra-run LLM/tool events via the tracer bridge).

This module is stateless; everything lives in the session / audit logger
it's constructed with.
"""

import logging
from dataclasses import dataclass
from typing import Any

from agents import Agent, Runner, RunConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.types import (
    AuditEvent,
    AuditEventType,
    ChannelKind,
    ChannelMessage,
    InvocationRequest,
    ManualTrigger,
    MessageRole,
    ScheduledTrigger,
    TriggerKind,
)
from jarvis.persistence.repositories import (
    ConversationRepo,
    MessageRepo,
    TriggerRepo,
)

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRunResult:
    final_output: str
    conversation_id: object  # UUID
    trigger_id: object  # UUID


class AgentRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        mcp_servers: list,
        llm_config: LLMConfig,
        model: Any = None,  # Override for tests; None means "use config.model"
        idle_timeout_sec: int = 900,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._mcp_servers = mcp_servers
        self._llm_config = llm_config
        self._model = model
        self._idle_timeout_sec = idle_timeout_sec

    async def run(self, request: InvocationRequest) -> AgentRunResult:
        channel_kind, channel_ref, prompt = _extract_from_trigger(request)
        trigger_kind = request.trigger.kind

        async with self._session_factory() as session:
            # Record the trigger.
            trig_repo = TriggerRepo(session)
            trig = await trig_repo.record(
                kind=trigger_kind.value,
                source_ref=_trigger_source_ref(request),
            )
            trigger_id = trig.id

            # Find-or-create the conversation.
            conv_repo = ConversationRepo(session)
            conv = await conv_repo.find_or_create_open(
                channel_kind=channel_kind,
                channel_ref=channel_ref,
                idle_timeout_sec=_idle_for_kind(
                    channel_kind, self._idle_timeout_sec
                ),
            )

            # Persist user message.
            msg_repo = MessageRepo(session)
            await msg_repo.append(
                conversation_id=conv.id,
                role=MessageRole.USER,
                content=prompt,
            )

        await self._audit.emit(
            AuditEvent(
                type=AuditEventType.TRIGGER_RECEIVED,
                conversation_id=conv.id,
                trigger_id=trigger_id,
                payload={
                    "trigger_kind": trigger_kind.value,
                    "channel_kind": channel_kind.value,
                    "channel_ref": channel_ref,
                },
            )
        )

        # Build the SDK agent.
        agent_kwargs: dict[str, Any] = {
            "name": "jarvis",
            "instructions": _system_prompt(),
            "mcp_servers": self._mcp_servers,
        }
        if self._model is not None:
            agent_kwargs["model"] = self._model
        else:
            agent_kwargs["model"] = self._llm_config.model
        agent = Agent(**agent_kwargs)

        sdk_result = await Runner.run(
            agent,
            prompt,
            run_config=RunConfig(workflow_name="jarvis-invoke"),
        )

        final_text = _extract_text(sdk_result)

        # Persist assistant message.
        async with self._session_factory() as session:
            await MessageRepo(session).append(
                conversation_id=conv.id,
                role=MessageRole.ASSISTANT,
                content=final_text,
            )

        return AgentRunResult(
            final_output=final_text,
            conversation_id=conv.id,
            trigger_id=trigger_id,
        )


def _extract_from_trigger(request: InvocationRequest):
    t = request.trigger
    if isinstance(t, ChannelMessage):
        return t.channel_kind, t.channel_ref, t.text
    if isinstance(t, ScheduledTrigger):
        return ChannelKind.SCHEDULED, t.schedule_id, t.prompt
    if isinstance(t, ManualTrigger):
        return ChannelKind.DASHBOARD, t.user, t.prompt
    raise ValueError(f"unknown trigger: {t!r}")


def _trigger_source_ref(request: InvocationRequest) -> str:
    t = request.trigger
    if isinstance(t, ChannelMessage):
        return t.external_id
    if isinstance(t, ScheduledTrigger):
        return t.schedule_id
    if isinstance(t, ManualTrigger):
        return t.user
    return "unknown"


def _idle_for_kind(kind: ChannelKind, default_sec: int) -> int:
    """Scheduled triggers always get a fresh conversation (spec §5.2)."""
    if kind == ChannelKind.SCHEDULED:
        return 0
    return default_sec


def _system_prompt() -> str:
    return (
        "You are Jarvis, a helpful personal assistant. "
        "Use the available MCP tools when they help answer the user. "
        "Be concise."
    )


def _extract_text(sdk_result) -> str:
    """Best-effort extraction of the final assistant text from SDK RunResult."""
    # SDK gives us `final_output` directly; stringify for safety.
    if hasattr(sdk_result, "final_output") and sdk_result.final_output is not None:
        return str(sdk_result.final_output)
    return ""
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_agent_runner.py -v`
Expected: 2 passed. If the SDK's `MessageOutputItem` / `ModelResponse` shapes differ in your installed version, adjust the test fake to match. The production code doesn't depend on these shapes (it uses `result.final_output`), only the test fake does.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 83 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/agents/runner.py tests/integration/test_agent_runner.py
git commit -m "add AgentRunner wrapping Agents SDK with persistence and auditing"
```

---

## Task 13: `TriggerDispatcher`

Design spec §5.2. Sole producer of `InvocationRequest`. Enforces:
- Dedup via LRU of recent `external_id`s (Discord gateway retry protection).
- Concurrency gate (semaphore — default 3 concurrent runs).
- Bridges trigger sources to `AgentRunner.run`.

Plan 2 doesn't yet have channel adapters calling into the dispatcher — that's Plan 3. But we build it now because `AgentRunner` needs a caller and the CLI entry (Task 14) goes through the dispatcher so manual-from-CLI shares the exact path Discord will use.

**Files:**
- Create: `jarvis/core/dispatcher.py`
- Create: `tests/integration/test_dispatcher.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_dispatcher.py`:

```python
import asyncio
import pytest
from agents import set_trace_processors

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.types import (
    ChannelKind,
    ChannelMessage,
    ManualTrigger,
)
from jarvis.persistence.db import Base, create_engine, session_factory


class _CountingFakeModel:
    """Counts how many times the SDK asked for a response."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(self, *a, **kw):
        from agents.items import MessageOutputItem, ModelResponse, Usage
        self.calls += 1
        return ModelResponse(
            output=[MessageOutputItem(text=f"reply-{self.calls}")],
            usage=Usage(),
            response_id=None,
        )


@pytest.fixture
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    set_trace_processors([JarvisTraceProcessor(audit)])

    yield engine, factory, audit

    await audit.stop()
    await engine.dispose()


async def test_dispatch_manual_trigger_runs(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(runner=runner, audit=audit)

    result = await dispatcher.dispatch_manual(user="mark", prompt="hi")
    assert "reply-1" in result.final_output
    assert model.calls == 1


async def test_dispatch_dedups_discord_message_by_external_id(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(runner=runner, audit=audit)

    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        text="same question",
        external_id="discord-msg-42",
    )
    first = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})
    second = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})

    assert first is not None
    assert second is None  # dedup suppressed
    assert model.calls == 1


async def test_dispatch_rejects_disallowed_discord_user(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(runner=runner, audit=audit)

    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="stranger",
        text="hi",
        external_id="msg-1",
    )
    result = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})
    assert result is None
    assert model.calls == 0


async def test_dispatch_concurrency_is_bounded(infra):
    _, factory, audit = infra

    class _SlowModel:
        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def get_response(self, *a, **kw):
            from agents.items import MessageOutputItem, ModelResponse, Usage
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.05)
            self.in_flight -= 1
            return ModelResponse(
                output=[MessageOutputItem(text="done")],
                usage=Usage(),
                response_id=None,
            )

    model = _SlowModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(
        runner=runner,
        audit=audit,
        max_concurrent=2,
    )

    # Kick off 5 concurrent manual runs; gate should cap in-flight at 2.
    tasks = [
        asyncio.create_task(dispatcher.dispatch_manual(user=f"u{i}", prompt="go"))
        for i in range(5)
    ]
    await asyncio.gather(*tasks)
    assert model.max_in_flight <= 2
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_dispatcher.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/core/dispatcher.py`**

```python
"""TriggerDispatcher — sole producer of InvocationRequest.

Cross-cutting policy lives here:
  - Allow-list enforcement for channel messages.
  - Dedup via bounded LRU on external_id (Discord gateway retry protection).
  - Concurrency gate (semaphore).

Every trigger path (Discord message, scheduled fire, manual) lands in
one of the `dispatch_*` methods and eventually calls `AgentRunner.run`.
"""

import asyncio
import logging
from collections import OrderedDict

from jarvis.agents.runner import AgentRunner, AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.core.types import (
    ChannelMessage,
    InvocationRequest,
    ManualTrigger,
    ScheduledTrigger,
)

_log = logging.getLogger(__name__)


class TriggerDispatcher:
    def __init__(
        self,
        *,
        runner: AgentRunner,
        audit: AuditLogger,
        max_concurrent: int = 3,
        dedup_window: int = 256,
    ) -> None:
        self._runner = runner
        self._audit = audit
        self._sem = asyncio.Semaphore(max_concurrent)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_cap = dedup_window

    async def dispatch_channel_message(
        self,
        msg: ChannelMessage,
        *,
        allowed_refs: set[str],
    ) -> AgentRunResult | None:
        """Dispatch a channel message. Returns None if rejected or a dup."""
        if msg.channel_ref not in allowed_refs:
            _log.info("rejected channel message from %r (not allow-listed)", msg.channel_ref)
            return None
        if msg.external_id in self._seen:
            _log.debug("dedup: suppressing repeat of %r", msg.external_id)
            return None
        self._remember(msg.external_id)

        return await self._run(InvocationRequest(trigger=msg))

    async def dispatch_scheduled(self, trigger: ScheduledTrigger) -> AgentRunResult:
        return await self._run(InvocationRequest(trigger=trigger))

    async def dispatch_manual(
        self,
        *,
        user: str,
        prompt: str,
    ) -> AgentRunResult:
        return await self._run(
            InvocationRequest(trigger=ManualTrigger(user=user, prompt=prompt)),
        )

    async def _run(self, request: InvocationRequest) -> AgentRunResult:
        async with self._sem:
            return await self._runner.run(request)

    def _remember(self, external_id: str) -> None:
        self._seen[external_id] = None
        # Bounded LRU: trim from the oldest.
        while len(self._seen) > self._seen_cap:
            self._seen.popitem(last=False)
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_dispatcher.py -v`
Expected: 4 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 87 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/core/dispatcher.py tests/integration/test_dispatcher.py
git commit -m "add TriggerDispatcher with allow-list, dedup, and concurrency gate"
```

---

## Task 14: `bootstrap()` wires everything together

Extend the Plan 1 bootstrap to start MCPManager, install the LLM client, install the tracer, and construct `AgentRunner` + `TriggerDispatcher`. Everything hangs off the returned `AppContext`.

**Files:**
- Modify: `jarvis/main.py`
- Modify: `tests/integration/test_main_smoke.py` (extend existing test)

- [ ] **Step 1: Rewrite `tests/integration/test_main_smoke.py`**

Replace the file contents with:

```python
import pytest

from jarvis.core.types import ManualTrigger
from jarvis.main import bootstrap


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "jarvis.yaml").write_text(
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: m
"""
    )
    (tmp_path / "channels.yaml").write_text("{}")
    (tmp_path / "mcp-servers.yaml").write_text("servers: []")
    return tmp_path


async def test_bootstrap_loads_config_and_initializes_db(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.config.jarvis.llm.model == "m"
        assert db_path.exists()
    finally:
        await ctx.shutdown()


async def test_bootstrap_exposes_runner_and_dispatcher(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.agent_runner is not None
        assert ctx.dispatcher is not None
        assert ctx.mcp_manager is not None
    finally:
        await ctx.shutdown()
```

- [ ] **Step 2: Run — confirm old test passes, new test fails**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: `test_bootstrap_exposes_runner_and_dispatcher` fails (AppContext has no `agent_runner` yet).

- [ ] **Step 3: Rewrite `jarvis/main.py`**

Replace the file contents with:

```python
"""Application bootstrap — wires persistence, audit, config, MCP, agent.

Returns an AppContext with every subsystem initialized. Later plans
(Discord, scheduler, dashboard) attach additional fields to this context.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from agents import set_trace_processors
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jarvis.agents.llm_client import build_llm_client, install_as_default
from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.loader import LoadedConfig, load_config
from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.mcp.manager import MCPManager
from jarvis.persistence.db import Base, create_engine, session_factory

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    config: LoadedConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    audit: AuditLogger
    mcp_manager: MCPManager
    agent_runner: AgentRunner
    dispatcher: TriggerDispatcher

    async def shutdown(self) -> None:
        await self.mcp_manager.stop()
        await self.audit.stop()
        await self.engine.dispose()


async def bootstrap(*, config_dir: Path | str, db_url: str) -> AppContext:
    cfg = load_config(config_dir)
    logging.basicConfig(level=cfg.jarvis.log_level)

    # DB.
    engine = create_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Audit.
    audit = AuditLogger(session_factory=factory)
    await audit.start()

    # Install the tracer BEFORE any Runner.run — replaces OpenAI's default.
    set_trace_processors([JarvisTraceProcessor(audit)])

    # LLM.
    llm_client = build_llm_client(cfg.jarvis.llm)
    install_as_default(llm_client)

    # MCP.
    mcp_manager = MCPManager(config=cfg.mcp_servers, session_factory=factory)
    await mcp_manager.start()

    # Agent + dispatcher.
    agent_runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=mcp_manager.agent_mcp_servers(),
        llm_config=cfg.jarvis.llm,
        idle_timeout_sec=cfg.jarvis.idle_timeout_sec,
    )
    dispatcher = TriggerDispatcher(
        runner=agent_runner,
        audit=audit,
        max_concurrent=cfg.jarvis.max_concurrent_agents,
    )

    _log.info("jarvis bootstrap complete")
    return AppContext(
        config=cfg,
        engine=engine,
        session_factory=factory,
        audit=audit,
        mcp_manager=mcp_manager,
        agent_runner=agent_runner,
        dispatcher=dispatcher,
    )
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 88 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/main.py tests/integration/test_main_smoke.py
git commit -m "extend bootstrap to wire MCPManager, AgentRunner, TriggerDispatcher"
```

---

## Task 15: `jarvis invoke` CLI

A small `typer` app with two subcommands:
- `jarvis invoke "<prompt>"` — bootstraps, dispatches a manual trigger, prints the result, shuts down.
- `jarvis check-config` — loads config, prints a summary, exits.

**Files:**
- Create: `jarvis/cli.py`
- Create: `jarvis/__main__.py` — makes `python -m jarvis` work.
- Create: `tests/integration/test_cli.py`

- [ ] **Step 1: Write failing tests**

Write `tests/integration/test_cli.py`:

```python
"""CLI smoke tests via typer's CliRunner + patched AppContext."""

import pytest
from typer.testing import CliRunner

from jarvis import cli


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "jarvis.yaml").write_text(
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: m
"""
    )
    (tmp_path / "channels.yaml").write_text("{}")
    (tmp_path / "mcp-servers.yaml").write_text("servers: []")
    return tmp_path


def test_check_config_prints_summary(config_dir):
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["check-config", "--config-dir", str(config_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "llm" in result.output.lower()
    assert "http://x/v1" in result.output


def test_invoke_requires_config_and_db(config_dir, tmp_path, monkeypatch):
    """The `invoke` command runs a manual dispatch and prints the output.

    We patch the LLM to avoid actually hitting a network endpoint.
    """
    # Monkeypatch AgentRunner.run to return a canned result.
    from jarvis.agents import runner as runner_mod

    async def _fake_run(self, request):
        return runner_mod.AgentRunResult(
            final_output="FAKE-CLI-OUTPUT",
            conversation_id="c",
            trigger_id="t",
        )

    monkeypatch.setattr(runner_mod.AgentRunner, "run", _fake_run)

    db_path = tmp_path / "jarvis.db"
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "invoke",
            "hello",
            "--config-dir", str(config_dir),
            "--db-url", f"sqlite+aiosqlite:///{db_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "FAKE-CLI-OUTPUT" in result.output
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_cli.py -v`
Expected: `ModuleNotFoundError` on `jarvis.cli`.

- [ ] **Step 3: Write `jarvis/cli.py`**

```python
"""CLI entry point for Jarvis.

Usage:
    python -m jarvis invoke "your prompt here"
    python -m jarvis check-config
"""

import asyncio
from pathlib import Path

import typer

from jarvis.main import bootstrap

app = typer.Typer(
    help="Jarvis personal agent CLI",
    add_completion=False,
    no_args_is_help=True,
)

_DEFAULT_CONFIG = Path("./config")
_DEFAULT_DB = "sqlite+aiosqlite:///./data/jarvis.db"


@app.command("invoke")
def invoke_command(
    prompt: str = typer.Argument(..., help="What to ask Jarvis"),
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
    db_url: str = typer.Option(
        _DEFAULT_DB, "--db-url", help="SQLAlchemy DB URL"
    ),
    user: str = typer.Option("cli", "--user", "-u", help="User identifier for the run"),
) -> None:
    """Run Jarvis once against a prompt and print the result."""
    asyncio.run(_invoke_async(prompt, config_dir, db_url, user))


async def _invoke_async(prompt: str, config_dir: Path, db_url: str, user: str) -> None:
    ctx = await bootstrap(config_dir=config_dir, db_url=db_url)
    try:
        result = await ctx.dispatcher.dispatch_manual(user=user, prompt=prompt)
        typer.echo(result.final_output)
    finally:
        await ctx.shutdown()


@app.command("check-config")
def check_config_command(
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
) -> None:
    """Validate and print a summary of the current config."""
    from jarvis.config.loader import load_config

    cfg = load_config(config_dir)
    typer.echo("=== jarvis config ===")
    typer.echo(f"llm.base_url       = {cfg.jarvis.llm.base_url}")
    typer.echo(f"llm.model          = {cfg.jarvis.llm.model}")
    typer.echo(f"timezone           = {cfg.jarvis.timezone}")
    typer.echo(f"idle_timeout_sec   = {cfg.jarvis.idle_timeout_sec}")
    typer.echo(f"max_concurrent     = {cfg.jarvis.max_concurrent_agents}")
    typer.echo(
        f"discord enabled    = {cfg.channels.discord is not None and cfg.channels.discord.enabled}"
    )
    typer.echo(f"mcp servers        = {len(cfg.mcp_servers.servers)}")
    for s in cfg.mcp_servers.servers:
        typer.echo(f"  - {s.name} ({s.transport}) enabled={s.enabled}")
```

- [ ] **Step 4: Create `jarvis/__main__.py`**

Write `jarvis/__main__.py`:

```python
"""Allow `python -m jarvis` to invoke the CLI."""

from jarvis.cli import app

if __name__ == "__main__":
    app()
```

- [ ] **Step 5: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_cli.py -v`
Expected: 2 passed.

- [ ] **Step 6: Manual smoke test** (optional but quick — verifies the wiring works end-to-end from a shell)

Run:
```bash
cd /Users/mdolton/dev/jarvis
mkdir -p /tmp/jarvis-smoke/config /tmp/jarvis-smoke/data
cat > /tmp/jarvis-smoke/config/jarvis.yaml <<'EOF'
llm:
  base_url: http://localhost:1234/v1
  api_key: dummy
  model: test-model
EOF
echo "{}" > /tmp/jarvis-smoke/config/channels.yaml
echo "servers: []" > /tmp/jarvis-smoke/config/mcp-servers.yaml

uv run python -m jarvis check-config \
  --config-dir /tmp/jarvis-smoke/config
```

Expected output: the check-config summary with `test-model` and `http://localhost:1234/v1`. The `invoke` command would require a real LLM endpoint to complete — it's tested via the fake model in step 5.

- [ ] **Step 7: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 90 passed, clean.

- [ ] **Step 8: Commit**

```bash
git add jarvis/cli.py jarvis/__main__.py tests/integration/test_cli.py
git commit -m "add jarvis CLI with invoke and check-config commands"
```

---

## Plan 2 complete — summary

At the end of Plan 2:

- `python -m jarvis check-config` validates and prints config.
- `python -m jarvis invoke "prompt"` runs an end-to-end agent turn: bootstraps, dispatches through the same `TriggerDispatcher` Plan 3's Discord adapter will use, records audit events and messages to SQLite, prints the final text.
- `MCPManager` connects to any configured MCP servers (stdio / http / sse), exposes them to the Agent, shadows the catalog + status to the DB.
- Every LLM call and tool call is recorded in `audit_events` via the `JarvisTraceProcessor` bridge.
- `ToolPolicy` classifies every tool as auto/confirm (used for recording in v2; v1 runs everything).
- Plan 1 debt items landed: `MessageRole` enum, `ConversationRepo.touch` wired, composite index, `MCPToolDescriptor`, `AuditLogger` bounded queue + drop counter, `AuditRepo.recent_as_events`.

**Known debt carried into Plan 3:**
- No retry on transient LLM/tool errors — a single failure ends the run.
- `OutputRouter` doesn't exist yet — the CLI prints directly. Discord (Plan 3) needs a real router.
- No `requires_confirmation` UI path — classifications are recorded but not acted on.
- MCP manager has no background reconnect yet — disconnected servers stay disconnected.

**Still to come:** Plan 3 Discord, Plan 4 Scheduler, Plan 5 Dashboard, Plan 6 Docker.

# Action Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a durable Action Inbox that pauses confirm-policy MCP tool calls, lets the operator approve or reject them from the dashboard, and resumes the original Jarvis run.

**Architecture:** Use the OpenAI Agents SDK local MCP `require_approval` support rather than a prompt-only proposal layer. Persist pending approval state in a new `actions` table, route all decision/resume work through an `ActionService`, and keep dashboard routes thin. Extract shared agent construction so both `AgentRunner` and `ActionService` build equivalent SDK agents.

**Tech Stack:** Python 3.12, OpenAI Agents SDK `RunState`, FastAPI, Jinja2, SQLAlchemy async, Alembic, pytest, ruff.

---

## File Structure

- Create `alembic/versions/0006_action_inbox.py` — creates/drops the `actions` table and indexes.
- Modify `jarvis/persistence/models.py` — add `ActionRow`.
- Modify `jarvis/persistence/repositories.py` — add `ActionRepo` status and query methods.
- Modify `jarvis/core/types.py` — add action audit event types.
- Modify `jarvis/mcp/tool_policy.py` — extend policy from two outcomes to runtime `allow` / `confirm` / `deny`.
- Create `jarvis/mcp/approval_policy.py` — database-backed SDK approval/filter callbacks.
- Modify `jarvis/mcp/manager.py` — build MCP SDK servers with approval policy and tool filter.
- Create `jarvis/agents/factory.py` — shared SDK `Agent` construction and model resolution helper.
- Modify `jarvis/agents/runner.py` — use the factory, detect approval interruptions, and create action rows.
- Create `jarvis/actions/serialization.py` — serialize/deserialize SDK `RunState` and `ToolApprovalItem`.
- Create `jarvis/actions/service.py` — approve/reject/resume workflow.
- Modify `jarvis/main.py` — wire `ActionService` into `AppContext` for dashboard approval routes.
- Create `jarvis/web/routes/actions.py` — list, detail, approve, reject routes.
- Create `jarvis/web/templates/actions.html` and `jarvis/web/templates/action_detail.html` — dashboard pages.
- Modify `jarvis/web/templates/base.html` — add Actions nav.
- Modify `jarvis/web/app.py` — include the actions router.
- Add/modify tests under `tests/unit` and `tests/integration` as listed per task.

## Task 1: Action Persistence and Migration

**Files:**
- Create: `alembic/versions/0006_action_inbox.py`
- Modify: `jarvis/persistence/models.py`
- Modify: `jarvis/persistence/repositories.py`
- Test: `tests/integration/test_action_migration.py`
- Test: `tests/integration/test_repositories_actions.py`
- Test: `tests/integration/test_orm_domain_tables.py`

- [ ] **Step 1: Add failing ORM/repository tests**

Append to `tests/integration/test_orm_domain_tables.py`:

```python
from jarvis.persistence.models import ActionRow


async def test_action_row_roundtrip(session):
    row = ActionRow(
        status="pending",
        decision=None,
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id="call-1",
        arguments_json={"to": "me@example.com"},
        run_state_json={"state": "serialized"},
        approval_item_json={"tool": "send_email"},
        model="test-model",
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()

    result = await session.execute(select(ActionRow))
    got = result.scalar_one()
    assert got.status == "pending"
    assert got.decision is None
    assert got.arguments_json == {"to": "me@example.com"}
    assert got.model == "test-model"
```

Create `tests/integration/test_repositories_actions.py`:

```python
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ActionRepo


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_action_repo_create_get_and_list_pending(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id="call-1",
        arguments_json={"to": "me@example.com"},
        run_state_json={"state": "serialized"},
        approval_item_json={"tool": "send_email"},
        model="test-model",
    )

    got = await repo.get(action.id)
    assert got is not None
    assert got.status == "pending"
    assert got.tool_name == "send_email"

    pending = await repo.list_pending()
    assert [a.id for a in pending] == [action.id]


async def test_action_repo_status_transitions(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=uuid4(),
        trigger_id=uuid4(),
        channel_kind="discord",
        channel_ref="123",
        server_name="calendar",
        tool_name="create_event",
        tool_call_id=None,
        arguments_json={},
        run_state_json={"state": "serialized"},
        approval_item_json={"tool": "create_event"},
        model="test-model",
    )

    await repo.mark_running(action.id, decision="approved", decision_reason=None)
    running = await repo.get(action.id)
    assert running.status == "running"
    assert running.decision == "approved"
    assert running.decided_at is not None

    await repo.mark_completed(action.id)
    completed = await repo.get(action.id)
    assert completed.status == "completed"
    assert completed.completed_at is not None


async def test_action_repo_rejects_non_pending_decision(session):
    repo = ActionRepo(session)
    action = await repo.create_pending(
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id=None,
        arguments_json={},
        run_state_json={},
        approval_item_json={},
        model="test-model",
    )
    await repo.mark_completed(action.id)

    try:
        await repo.mark_running(action.id, decision="approved", decision_reason=None)
    except ValueError as exc:
        assert "pending" in str(exc)
    else:
        raise AssertionError("expected non-pending action to be rejected")
```

Create `tests/integration/test_action_migration.py`:

```python
import os
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "-x",
            f"db_url=sqlite+aiosqlite:///{db_path}",
            *cmd.split(),
        ],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ},
    )


def test_action_inbox_migration_roundtrip(tmp_path):
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr

    down = _run_alembic(db_path, "downgrade 0005")
    assert down.returncode == 0, down.stderr

    up_again = _run_alembic(db_path, "upgrade head")
    assert up_again.returncode == 0, up_again.stderr
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/integration/test_orm_domain_tables.py::test_action_row_roundtrip tests/integration/test_repositories_actions.py tests/integration/test_action_migration.py -q
```

Expected: fails because `ActionRow`, `ActionRepo`, and migration `0006` do not exist.

- [ ] **Step 3: Add `ActionRow`**

In `jarvis/persistence/models.py`, add `ActionRow` after `AuditEventRow`:

```python
class ActionRow(Base):
    __tablename__ = "actions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(32), index=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel_kind: Mapped[str] = mapped_column(String(32))
    channel_ref: Mapped[str] = mapped_column(String(128))
    server_name: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict)
    run_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_item_json: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_actions_status_created_at", "status", "created_at"),
    )
```

Ensure `Index` is already imported from `sqlalchemy`; if not, keep the existing import shape:

```python
from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, String, Text
```

- [ ] **Step 4: Add Alembic migration**

Create `alembic/versions/0006_action_inbox.py`:

```python
"""add action inbox

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        sa.Column("channel_kind", sa.String(length=32), nullable=False),
        sa.Column("channel_ref", sa.String(length=128), nullable=False),
        sa.Column("server_name", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=True),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("run_state_json", sa.JSON(), nullable=False),
        sa.Column("approval_item_json", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_actions_status", "actions", ["status"], unique=False)
    op.create_index("ix_actions_created_at", "actions", ["created_at"], unique=False)
    op.create_index(
        "ix_actions_status_created_at",
        "actions",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index("ix_actions_conversation_id", "actions", ["conversation_id"], unique=False)
    op.create_index("ix_actions_trigger_id", "actions", ["trigger_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_actions_trigger_id", table_name="actions")
    op.drop_index("ix_actions_conversation_id", table_name="actions")
    op.drop_index("ix_actions_status_created_at", table_name="actions")
    op.drop_index("ix_actions_created_at", table_name="actions")
    op.drop_index("ix_actions_status", table_name="actions")
    op.drop_table("actions")
```

- [ ] **Step 5: Add `ActionRepo`**

In `jarvis/persistence/repositories.py`, import `ActionRow` and append:

```python
class ActionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_pending(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        channel_kind: str,
        channel_ref: str,
        server_name: str,
        tool_name: str,
        tool_call_id: str | None,
        arguments_json: dict,
        run_state_json: dict,
        approval_item_json: dict,
        model: str,
    ) -> ActionRow:
        row = ActionRow(
            status="pending",
            decision=None,
            conversation_id=conversation_id,
            trigger_id=trigger_id,
            channel_kind=channel_kind,
            channel_ref=channel_ref,
            server_name=server_name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments_json=arguments_json,
            run_state_json=run_state_json,
            approval_item_json=approval_item_json,
            model=model,
            created_at=_utcnow(),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get(self, action_id: UUID) -> ActionRow | None:
        return await self._session.get(ActionRow, action_id)

    async def list_pending(self, *, limit: int = 100) -> list[ActionRow]:
        result = await self._session.execute(
            select(ActionRow)
            .where(ActionRow.status == "pending")
            .order_by(ActionRow.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def list_recent(self, *, limit: int = 100) -> list[ActionRow]:
        result = await self._session.execute(
            select(ActionRow).order_by(ActionRow.created_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def mark_running(
        self,
        action_id: UUID,
        *,
        decision: str,
        decision_reason: str | None,
    ) -> None:
        row = await self.get(action_id)
        if row is None:
            raise ValueError(f"action {action_id} not found")
        if row.status != "pending":
            raise ValueError(f"action {action_id} is not pending")
        row.status = "running"
        row.decision = decision
        row.decision_reason = decision_reason
        row.decided_at = _utcnow()
        await self._session.commit()

    async def mark_completed(self, action_id: UUID) -> None:
        await self._session.execute(
            update(ActionRow)
            .where(ActionRow.id == action_id)
            .values(status="completed", completed_at=_utcnow(), error=None)
        )
        await self._session.commit()

    async def mark_failed(self, action_id: UUID, error: str) -> None:
        await self._session.execute(
            update(ActionRow)
            .where(ActionRow.id == action_id)
            .values(status="failed", completed_at=_utcnow(), error=error)
        )
        await self._session.commit()
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest tests/integration/test_orm_domain_tables.py::test_action_row_roundtrip tests/integration/test_repositories_actions.py tests/integration/test_action_migration.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0006_action_inbox.py jarvis/persistence/models.py jarvis/persistence/repositories.py tests/integration/test_action_migration.py tests/integration/test_orm_domain_tables.py tests/integration/test_repositories_actions.py
git commit -m "feat: add action inbox persistence"
```

## Task 2: Runtime Tool Policy and MCP Approval Wiring

**Files:**
- Modify: `jarvis/mcp/tool_policy.py`
- Create: `jarvis/mcp/approval_policy.py`
- Modify: `jarvis/mcp/manager.py`
- Test: `tests/unit/test_tool_policy.py`
- Test: `tests/integration/test_mcp_approval_policy.py`
- Test: `tests/integration/test_mcp_manager.py`

- [ ] **Step 1: Add failing policy tests**

Append to `tests/unit/test_tool_policy.py`:

```python
from jarvis.mcp.tool_policy import RuntimeToolDecision, runtime_decision


def test_runtime_override_allow_confirm_deny():
    t = _desc(name="delete_event", destructive_hint=True)
    assert runtime_decision(t, override="allow") == RuntimeToolDecision.ALLOW
    assert runtime_decision(t, override="confirm") == RuntimeToolDecision.CONFIRM
    assert runtime_decision(t, override="deny") == RuntimeToolDecision.DENY


def test_runtime_auto_detect_maps_classifier():
    assert runtime_decision(_desc(name="list_events"), override=None) == RuntimeToolDecision.ALLOW
    assert runtime_decision(_desc(name="send_email"), override=None) == RuntimeToolDecision.CONFIRM
```

Create `tests/integration/test_mcp_approval_policy.py`:

```python
from types import SimpleNamespace

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


async def _seed(factory, *, override: str | None):
    async with factory() as s:
        server = await MCPServerRepo(s).upsert(name="gmail", transport="http")
        await MCPToolRepo(s).replace_for_server(
            server.id,
            tools=[
                MCPToolDescriptor(name="list_messages", input_schema={}, read_only_hint=True),
                MCPToolDescriptor(name="send_email", input_schema={}, destructive_hint=True),
            ],
        )
        tools = await MCPToolRepo(s).list_for_server(server.id)
        for tool in tools:
            if tool.name == "send_email":
                await MCPToolRepo(s).set_policy_override(tool.id, override)


async def test_needs_approval_for_confirm_tool(factory):
    await _seed(factory, override="confirm")
    policy = MCPApprovalPolicy(session_factory=factory)
    tool = SimpleNamespace(name="send_email")
    assert await policy.needs_approval("gmail", tool) is True


async def test_no_approval_for_allowed_read_tool(factory):
    await _seed(factory, override=None)
    policy = MCPApprovalPolicy(session_factory=factory)
    tool = SimpleNamespace(name="list_messages")
    assert await policy.needs_approval("gmail", tool) is False


async def test_filter_blocks_denied_tool(factory):
    await _seed(factory, override="deny")
    policy = MCPApprovalPolicy(session_factory=factory)
    allowed = await policy.filter_tool("gmail", SimpleNamespace(name="list_messages"))
    denied = await policy.filter_tool("gmail", SimpleNamespace(name="send_email"))
    assert allowed is True
    assert denied is False
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/unit/test_tool_policy.py tests/integration/test_mcp_approval_policy.py -q
```

Expected: fails because `RuntimeToolDecision`, `runtime_decision`, and `MCPApprovalPolicy` do not exist.

- [ ] **Step 3: Extend `tool_policy.py`**

Replace `ToolPolicy` and `classify` with backward-compatible definitions:

```python
class ToolPolicy(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"


class RuntimeToolDecision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"
```

Keep `classify(...)` returning `ToolPolicy` so existing tests continue to pass. Add:

```python
def runtime_decision(
    tool: MCPToolDescriptor,
    *,
    override: str | None = None,
) -> RuntimeToolDecision:
    if override == "allow":
        return RuntimeToolDecision.ALLOW
    if override == "confirm":
        return RuntimeToolDecision.CONFIRM
    if override == "deny":
        return RuntimeToolDecision.DENY

    policy = classify(tool, override=None)
    if policy == ToolPolicy.AUTO:
        return RuntimeToolDecision.ALLOW
    return RuntimeToolDecision.CONFIRM
```

Also update `classify(...)` so `override="allow"` is treated like `AUTO` for legacy callers:

```python
if override in ("auto", "allow"):
    return ToolPolicy.AUTO
if override == "confirm":
    return ToolPolicy.CONFIRM
```

- [ ] **Step 4: Implement `MCPApprovalPolicy`**

Create `jarvis/mcp/approval_policy.py`:

```python
from __future__ import annotations

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
        decision = await self._decision(server_name, tool)
        return decision == RuntimeToolDecision.CONFIRM

    async def filter_tool(self, server_name: str, tool: Any) -> bool:
        decision = await self._decision(server_name, tool)
        return decision != RuntimeToolDecision.DENY

    async def _decision(self, server_name: str, tool: Any) -> RuntimeToolDecision:
        descriptor, override = await self._lookup(server_name, tool)
        return runtime_decision(descriptor, override=override)

    async def _lookup(self, server_name: str, tool: Any) -> tuple[MCPToolDescriptor, str | None]:
        tool_name = str(getattr(tool, "name"))
        async with self._session_factory() as session:
            result = await session.execute(
                select(MCPToolRow)
                .join(MCPServerRow, MCPToolRow.server_id == MCPServerRow.id)
                .where(MCPServerRow.name == server_name, MCPToolRow.name == tool_name)
            )
            row = result.scalar_one_or_none()

        if row is None:
            descriptor = MCPToolDescriptor(name=tool_name, input_schema={})
            return descriptor, None

        descriptor = MCPToolDescriptor(
            name=row.name,
            description=row.description,
            input_schema=row.input_schema,
            read_only_hint=row.read_only_hint,
            destructive_hint=row.destructive_hint,
        )
        return descriptor, row.policy_override
```

- [ ] **Step 5: Wire manager server construction**

Modify `MCPManager.__init__` to create:

```python
from jarvis.mcp.approval_policy import MCPApprovalPolicy

self._approval_policy = MCPApprovalPolicy(session_factory=session_factory)
```

Pass the policy to all builders:

```python
sdk_server = _build_sdk_server(cfg, approval_policy=self._approval_policy)
```

```python
new_sdk = _build_streamable_http(
    url,
    headers,
    name=provider_key,
    approval_policy=self._approval_policy,
)
```

Change `_build_streamable_http` signature:

```python
def _build_streamable_http(
    url: str,
    headers: dict[str, str],
    *,
    name: str,
    approval_policy: MCPApprovalPolicy | None = None,
) -> object:
```

Build kwargs:

```python
kwargs = {}
if approval_policy is not None:
    kwargs["require_approval"] = lambda ctx, agent, tool: approval_policy.needs_approval(name, tool)
    kwargs["tool_filter"] = lambda ctx, agent, tool: approval_policy.filter_tool(name, tool)
return MCPServerStreamableHttp(
    name=name,
    params={"url": url, "headers": headers, "timeout": 30},
    cache_tools_list=True,
    client_session_timeout_seconds=30,
    max_retry_attempts=2,
    retry_backoff_seconds_base=1.0,
    **kwargs,
)
```

Change `_build_sdk_server(cfg)` to `_build_sdk_server(cfg, *, approval_policy=None)` and pass the same `kwargs` to `MCPServerStdio`, `MCPServerStreamableHttp`, and `MCPServerSse`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest tests/unit/test_tool_policy.py tests/integration/test_mcp_approval_policy.py tests/integration/test_mcp_manager.py -q
```

Expected: all selected tests pass. If existing monkeypatches for `_build_streamable_http` use the old signature, update test lambdas to accept `**kwargs`.

- [ ] **Step 7: Commit**

```bash
git add jarvis/mcp/tool_policy.py jarvis/mcp/approval_policy.py jarvis/mcp/manager.py tests/unit/test_tool_policy.py tests/integration/test_mcp_approval_policy.py tests/integration/test_mcp_manager.py
git commit -m "feat: wire MCP approval policy"
```

## Task 3: Shared Agent Factory and Runner Interruption Persistence

**Files:**
- Create: `jarvis/agents/factory.py`
- Create: `jarvis/actions/serialization.py`
- Modify: `jarvis/agents/runner.py`
- Modify: `jarvis/core/types.py`
- Test: `tests/integration/test_agent_runner_actions.py`
- Test: `tests/unit/test_action_serialization.py`

- [ ] **Step 1: Add failing serialization unit test**

Create `tests/unit/test_action_serialization.py`:

```python
from types import SimpleNamespace

from jarvis.actions.serialization import approval_item_to_json


def test_approval_item_to_json_extracts_tool_metadata():
    approval = SimpleNamespace(
        raw_item=SimpleNamespace(
            name="send_email",
            call_id="call-1",
            arguments='{"to":"me@example.com"}',
            server_label="gmail",
        )
    )

    payload = approval_item_to_json(approval)
    assert payload["tool_name"] == "send_email"
    assert payload["tool_call_id"] == "call-1"
    assert payload["arguments_json"] == {"to": "me@example.com"}
    assert payload["server_name"] == "gmail"
```

- [ ] **Step 2: Add failing runner interruption test**

Create `tests/integration/test_agent_runner_actions.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.types import InvocationRequest, ManualTrigger
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ActionRepo, MessageRepo


class _FakeRunState:
    def to_json(self):
        return {"state": "serialized"}


class _FakeResult:
    final_output = None
    interruptions = [
        SimpleNamespace(
            raw_item=SimpleNamespace(
                name="send_email",
                call_id="call-1",
                arguments='{"to":"me@example.com"}',
                server_label="gmail",
            )
        )
    ]
    state = _FakeRunState()


@pytest.fixture
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    yield factory, audit
    await audit.stop()
    await engine.dispose()


async def test_runner_creates_pending_action_on_tool_approval(monkeypatch, infra):
    factory, audit = infra
    run_mock = AsyncMock(return_value=_FakeResult())
    monkeypatch.setattr("jarvis.agents.runner.Runner.run", run_mock)

    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model_provider=lambda: "m",
    )

    result = await runner.run(InvocationRequest(trigger=ManualTrigger(user="dashboard", prompt="send it")))
    assert "Action approval required" in result.final_output

    async with factory() as s:
        actions = await ActionRepo(s).list_pending()
        assert len(actions) == 1
        assert actions[0].server_name == "gmail"
        assert actions[0].tool_name == "send_email"
        assert actions[0].arguments_json == {"to": "me@example.com"}
        msgs = await MessageRepo(s).history(actions[0].conversation_id)
        assert msgs[-1].content == result.final_output
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
uv run pytest tests/unit/test_action_serialization.py tests/integration/test_agent_runner_actions.py -q
```

Expected: fails because action serialization and interruption handling do not exist.

- [ ] **Step 4: Add audit event types**

In `jarvis/core/types.py`, add:

```python
    ACTION_CREATED = "action.created"
    ACTION_APPROVED = "action.approved"
    ACTION_REJECTED = "action.rejected"
    ACTION_COMPLETED = "action.completed"
    ACTION_FAILED = "action.failed"
```

- [ ] **Step 5: Add shared agent factory**

Create `jarvis/agents/factory.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agents import Agent

from jarvis.config.schema import LLMConfig
from jarvis.core.types import ScheduledTrigger


def system_prompt() -> str:
    return (
        "You are Jarvis, a helpful personal assistant. "
        "Use the available MCP tools when they help answer the user. "
        "Be concise."
    )


def resolve_model(trigger, *, explicit, model_provider: Callable[[], str] | None, config_default: str):
    if explicit is not None:
        return explicit
    if isinstance(trigger, ScheduledTrigger) and trigger.model:
        return trigger.model
    if model_provider is not None:
        return model_provider()
    return config_default


def build_agent(
    *,
    llm_config: LLMConfig,
    mcp_servers_provider: Callable[[], list],
    trigger=None,
    explicit_model: Any = None,
    model_provider: Callable[[], str] | None = None,
    model_override: str | None = None,
) -> tuple[Agent, str]:
    model = model_override or resolve_model(
        trigger,
        explicit=explicit_model,
        model_provider=model_provider,
        config_default=llm_config.model,
    )
    agent = Agent(
        name="jarvis",
        instructions=system_prompt(),
        mcp_servers=mcp_servers_provider(),
        model=model,
    )
    return agent, str(model)
```

Update `jarvis/agents/runner.py` to import `build_agent` and `resolve_model` from this file. Remove the local `_system_prompt` and `resolve_model` implementations or re-export `resolve_model` for the existing `tests/unit/test_resolve_model.py`.

- [ ] **Step 6: Add serialization helpers**

Create `jarvis/actions/serialization.py`:

```python
from __future__ import annotations

import json
from typing import Any

from agents.items import ToolApprovalItem
from agents.run_state import RunState


def run_state_to_json(state: RunState) -> dict[str, Any]:
    return state.to_json()


async def run_state_from_json(agent, payload: dict[str, Any]) -> RunState:
    return await RunState.from_json(agent, payload)


def approval_item_to_json(approval_item: ToolApprovalItem | Any) -> dict[str, Any]:
    raw = getattr(approval_item, "raw_item", approval_item)
    if hasattr(raw, "model_dump"):
        raw_payload = raw.model_dump(exclude_unset=True)
    elif isinstance(raw, dict):
        raw_payload = dict(raw)
    else:
        raw_payload = dict(getattr(raw, "__dict__", {}))

    tool_name = (
        raw_payload.get("name")
        or raw_payload.get("tool_name")
        or getattr(approval_item, "tool_name", None)
        or "unknown"
    )
    call_id = raw_payload.get("call_id") or raw_payload.get("id")
    server_name = (
        raw_payload.get("server_label")
        or raw_payload.get("server_name")
        or raw_payload.get("server")
        or raw_payload.get("namespace")
        or "unknown"
    )
    arguments = raw_payload.get("arguments") or raw_payload.get("arguments_json") or {}
    if isinstance(arguments, str):
        try:
            arguments_json = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments_json = {"raw": arguments}
    elif isinstance(arguments, dict):
        arguments_json = arguments
    else:
        arguments_json = {"value": arguments}

    return {
        "raw_item": raw_payload,
        "server_name": str(server_name),
        "tool_name": str(tool_name),
        "tool_call_id": str(call_id) if call_id is not None else None,
        "arguments_json": arguments_json,
    }
```

- [ ] **Step 7: Update `AgentRunner`**

Modify `AgentRunner.run()`:

1. Replace local agent construction with:

```python
agent, resolved_model = build_agent(
    llm_config=self._llm_config,
    mcp_servers_provider=self._mcp_servers_provider,
    trigger=request.trigger,
    explicit_model=self._model,
    model_provider=self._model_provider,
)
```

2. After `sdk_result = await Runner.run(...)`, add:

```python
interruptions = list(getattr(sdk_result, "interruptions", []) or [])
if interruptions:
    approval_payload = approval_item_to_json(interruptions[0])
    run_state = getattr(sdk_result, "state", None) or getattr(sdk_result, "_run_state", None)
    if run_state is None:
        raise RuntimeError("approval interruption did not include a serializable run state")

    async with self._session_factory() as session:
        action = await ActionRepo(session).create_pending(
            conversation_id=conv_id,
            trigger_id=trigger_id,
            channel_kind=channel_kind.value,
            channel_ref=channel_ref,
            server_name=approval_payload["server_name"],
            tool_name=approval_payload["tool_name"],
            tool_call_id=approval_payload["tool_call_id"],
            arguments_json=approval_payload["arguments_json"],
            run_state_json=run_state_to_json(run_state),
            approval_item_json=approval_payload,
            model=resolved_model,
        )
        final_text = (
            f"Action approval required: {action.server_name}.{action.tool_name} "
            f"({action.id}). Review it in the Action Inbox."
        )
        await MessageRepo(session).append(
            conversation_id=conv_id,
            role=MessageRole.ASSISTANT,
            content=final_text,
        )

    await self._audit.emit(
        AuditEvent(
            type=AuditEventType.ACTION_CREATED,
            conversation_id=conv_id,
            trigger_id=trigger_id,
            payload={
                "action_id": str(action.id),
                "server_name": action.server_name,
                "tool_name": action.tool_name,
            },
        )
    )

    return AgentRunResult(
        final_output=final_text,
        conversation_id=conv_id,
        trigger_id=trigger_id,
        channel_kind=channel_kind,
        channel_ref=channel_ref,
    )
```

Import `ActionRepo`, `approval_item_to_json`, and `run_state_to_json`.

- [ ] **Step 8: Run focused tests**

Run:

```bash
uv run pytest tests/unit/test_action_serialization.py tests/unit/test_resolve_model.py tests/integration/test_agent_runner.py tests/integration/test_agent_runner_actions.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```bash
git add jarvis/agents/factory.py jarvis/actions/serialization.py jarvis/agents/runner.py jarvis/core/types.py tests/unit/test_action_serialization.py tests/integration/test_agent_runner_actions.py
git commit -m "feat: persist interrupted tool approvals"
```

## Task 4: Action Service Resume Workflow

**Files:**
- Create: `jarvis/actions/__init__.py`
- Create: `jarvis/actions/service.py`
- Modify: `jarvis/main.py`
- Test: `tests/integration/test_action_service.py`

- [ ] **Step 1: Add failing service tests**

Create `tests/integration/test_action_service.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jarvis.actions.service import ActionService
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.types import ChannelKind
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ActionRepo, MessageRepo


class _FakeRunState:
    def __init__(self):
        self.approved = False
        self.rejected = False

    def approve(self, item):
        self.approved = True

    def reject(self, item, *, rejection_message=None):
        self.rejected = True
        self.rejection_message = rejection_message


class _FakeResult:
    final_output = "resume complete"
    interruptions = []


@pytest.fixture
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    yield factory, audit
    await audit.stop()
    await engine.dispose()


async def _action(factory):
    async with factory() as s:
        repo = ActionRepo(s)
        return await repo.create_pending(
            conversation_id=None,
            trigger_id=None,
            channel_kind=ChannelKind.DASHBOARD.value,
            channel_ref="dashboard",
            server_name="gmail",
            tool_name="send_email",
            tool_call_id="call-1",
            arguments_json={"to": "me@example.com"},
            run_state_json={"state": "serialized"},
            approval_item_json={"raw_item": {"name": "send_email"}},
            model="test-model",
        )


async def test_approve_marks_completed_and_routes(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory)
    state = _FakeRunState()
    monkeypatch.setattr("jarvis.actions.service.run_state_from_json", AsyncMock(return_value=state))
    monkeypatch.setattr("jarvis.actions.service.approval_item_from_json", lambda agent, payload: SimpleNamespace(raw_item=payload["raw_item"]))
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))
    router = SimpleNamespace(route=AsyncMock())

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=router,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    await service.approve(action.id)
    assert state.approved is True
    router.route.assert_awaited_once()

    async with factory() as s:
        got = await ActionRepo(s).get(action.id)
        assert got.status == "completed"
        assert got.decision == "approved"


async def test_reject_sends_reason_to_run_state(monkeypatch, infra):
    factory, audit = infra
    action = await _action(factory)
    state = _FakeRunState()
    monkeypatch.setattr("jarvis.actions.service.run_state_from_json", AsyncMock(return_value=state))
    monkeypatch.setattr("jarvis.actions.service.approval_item_from_json", lambda agent, payload: SimpleNamespace(raw_item=payload["raw_item"]))
    monkeypatch.setattr("jarvis.actions.service.Runner.run", AsyncMock(return_value=_FakeResult()))

    service = ActionService(
        session_factory=factory,
        audit=audit,
        output_router=None,
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        mcp_servers_provider=lambda: [],
    )

    await service.reject(action.id, reason="Do not send this.")
    assert state.rejected is True
    assert state.rejection_message == "Do not send this."
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
uv run pytest tests/integration/test_action_service.py -q
```

Expected: fails because `ActionService` and `approval_item_from_json` do not exist.

- [ ] **Step 3: Add approval item deserialization**

Append to `jarvis/actions/serialization.py`:

```python
def approval_item_from_json(agent, payload: dict[str, Any]):
    from agents.items import ToolApprovalItem

    raw = payload.get("raw_item", payload)
    return ToolApprovalItem(agent=agent, raw_item=raw)
```

- [ ] **Step 4: Implement `ActionService`**

Create `jarvis/actions/__init__.py` as an empty package marker.

Create `jarvis/actions/service.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from agents import RunConfig, Runner
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.actions.serialization import approval_item_from_json, run_state_from_json
from jarvis.agents.factory import build_agent
from jarvis.agents.runner import AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.output_router import OutputRouter
from jarvis.core.types import AuditEvent, AuditEventType, ChannelKind, MessageRole
from jarvis.persistence.repositories import ActionRepo, MessageRepo


class ActionService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        output_router: OutputRouter | None,
        llm_config: LLMConfig,
        mcp_servers_provider: Callable[[], list],
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._output_router = output_router
        self._llm_config = llm_config
        self._mcp_servers_provider = mcp_servers_provider

    async def approve(self, action_id: UUID) -> AgentRunResult:
        return await self._decide(action_id, decision="approved", reason=None)

    async def reject(self, action_id: UUID, *, reason: str | None = None) -> AgentRunResult:
        return await self._decide(
            action_id,
            decision="rejected",
            reason=reason or "Tool execution was not approved.",
        )

    async def _decide(
        self,
        action_id: UUID,
        *,
        decision: str,
        reason: str | None,
    ) -> AgentRunResult:
        async with self._session_factory() as session:
            repo = ActionRepo(session)
            action = await repo.get(action_id)
            if action is None:
                raise ValueError(f"action {action_id} not found")
            await repo.mark_running(action_id, decision=decision, decision_reason=reason)

        agent, _ = build_agent(
            llm_config=self._llm_config,
            mcp_servers_provider=self._mcp_servers_provider,
            model_override=action.model,
        )

        try:
            run_state = await run_state_from_json(agent, action.run_state_json)
            approval_item = approval_item_from_json(agent, action.approval_item_json)
            if decision == "approved":
                run_state.approve(approval_item)
                event_type = AuditEventType.ACTION_APPROVED
            else:
                run_state.reject(approval_item, rejection_message=reason)
                event_type = AuditEventType.ACTION_REJECTED

            await self._audit.emit(
                AuditEvent(
                    type=event_type,
                    conversation_id=action.conversation_id,
                    trigger_id=action.trigger_id,
                    payload={
                        "action_id": str(action.id),
                        "server_name": action.server_name,
                        "tool_name": action.tool_name,
                    },
                )
            )

            sdk_result = await Runner.run(
                agent,
                run_state,
                run_config=RunConfig(workflow_name="jarvis-action-resume"),
            )
            final_text = str(getattr(sdk_result, "final_output", "") or "")

            async with self._session_factory() as session:
                if action.conversation_id is not None:
                    await MessageRepo(session).append(
                        conversation_id=action.conversation_id,
                        role=MessageRole.ASSISTANT,
                        content=final_text,
                    )
                await ActionRepo(session).mark_completed(action_id)

            await self._audit.emit(
                AuditEvent(
                    type=AuditEventType.ACTION_COMPLETED,
                    conversation_id=action.conversation_id,
                    trigger_id=action.trigger_id,
                    payload={"action_id": str(action.id)},
                )
            )

            result = AgentRunResult(
                final_output=final_text,
                conversation_id=action.conversation_id,
                trigger_id=action.trigger_id,
                channel_kind=ChannelKind(action.channel_kind),
                channel_ref=action.channel_ref,
            )
            if self._output_router is not None:
                await self._output_router.route(result)
            return result
        except Exception as exc:
            async with self._session_factory() as session:
                await ActionRepo(session).mark_failed(action_id, f"{type(exc).__name__}: {exc}")
            await self._audit.emit(
                AuditEvent(
                    type=AuditEventType.ACTION_FAILED,
                    conversation_id=action.conversation_id,
                    trigger_id=action.trigger_id,
                    payload={"action_id": str(action.id), "error": str(exc)},
                )
            )
            raise
```

- [ ] **Step 5: Wire service into main and scheduler**

In `jarvis/main.py`, import `ActionService`, add `action_service: ActionService` to `AppContext`, and instantiate after `output_router`:

```python
action_service = ActionService(
    session_factory=factory,
    audit=audit,
    output_router=output_router,
    llm_config=cfg.jarvis.llm,
    mcp_servers_provider=mcp_manager.agent_mcp_servers,
)
```

The scheduler-owned `AgentRunner` already goes through the same interruption handling as interactive runs because Task 3 changed `AgentRunner` itself. No scheduler-specific action service wiring is needed for v1.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest tests/integration/test_action_service.py tests/integration/test_main_smoke.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add jarvis/actions/__init__.py jarvis/actions/service.py jarvis/actions/serialization.py jarvis/main.py tests/integration/test_action_service.py
git commit -m "feat: resume action inbox decisions"
```

## Task 5: Actions Dashboard

**Files:**
- Create: `jarvis/web/routes/actions.py`
- Create: `jarvis/web/templates/actions.html`
- Create: `jarvis/web/templates/action_detail.html`
- Modify: `jarvis/web/app.py`
- Modify: `jarvis/web/templates/base.html`
- Test: `tests/integration/test_web_actions.py`

- [ ] **Step 1: Add failing web tests**

Create `tests/integration/test_web_actions.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ActionRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        action = await ActionRepo(s).create_pending(
            conversation_id=None,
            trigger_id=None,
            channel_kind="dashboard",
            channel_ref="dashboard",
            server_name="gmail",
            tool_name="send_email",
            tool_call_id="call-1",
            arguments_json={"to": "me@example.com"},
            run_state_json={"state": "serialized"},
            approval_item_json={"raw_item": {"name": "send_email"}},
            model="test-model",
        )

    ctx = SimpleNamespace(
        session_factory=factory,
        action_service=SimpleNamespace(approve=AsyncMock(), reject=AsyncMock()),
    )
    app = create_app(app_context=ctx)
    yield TestClient(app), action.id, ctx
    await engine.dispose()


def test_actions_page_lists_pending_action(client):
    c, action_id, _ = client
    resp = c.get("/actions")
    assert resp.status_code == 200
    assert "send_email" in resp.text
    assert str(action_id) in resp.text


def test_action_detail_renders_arguments(client):
    c, action_id, _ = client
    resp = c.get(f"/actions/{action_id}")
    assert resp.status_code == 200
    assert "me@example.com" in resp.text
    assert 'action="{}"'.format(f"/actions/{action_id}/approve") in resp.text


def test_approve_posts_to_service(client):
    c, action_id, ctx = client
    resp = c.post(f"/actions/{action_id}/approve", follow_redirects=False)
    assert resp.status_code in (302, 303)
    ctx.action_service.approve.assert_awaited_once_with(action_id)


def test_reject_posts_to_service(client):
    c, action_id, ctx = client
    resp = c.post(
        f"/actions/{action_id}/reject",
        data={"reason": "No"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    ctx.action_service.reject.assert_awaited_once_with(action_id, reason="No")
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
uv run pytest tests/integration/test_web_actions.py -q
```

Expected: fails because `/actions` routes/templates do not exist.

- [ ] **Step 3: Add routes**

Create `jarvis/web/routes/actions.py`:

```python
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import ActionRepo

router = APIRouter()


@router.get("/actions", response_class=HTMLResponse)
async def actions_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        actions = await ActionRepo(session).list_recent(limit=100)
    return templates.TemplateResponse(request, "actions.html", {"actions": actions})


@router.get("/actions/{action_id}", response_class=HTMLResponse)
async def action_detail(request: Request, action_id: UUID):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        action = await ActionRepo(session).get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    return templates.TemplateResponse(
        request,
        "action_detail.html",
        {
            "action": action,
            "arguments_pretty": json.dumps(action.arguments_json, indent=2, sort_keys=True),
        },
    )


@router.post("/actions/{action_id}/approve")
async def approve_action(request: Request, action_id: UUID):
    await request.app.state.ctx.action_service.approve(action_id)
    return RedirectResponse(url=f"/actions/{action_id}", status_code=303)


@router.post("/actions/{action_id}/reject")
async def reject_action(request: Request, action_id: UUID, reason: str = Form("")):
    await request.app.state.ctx.action_service.reject(action_id, reason=reason.strip() or None)
    return RedirectResponse(url=f"/actions/{action_id}", status_code=303)
```

- [ ] **Step 4: Add templates and nav**

Create `jarvis/web/templates/actions.html`:

```html
{% extends "base.html" %}
{% block title %}Actions{% endblock %}
{% block content %}
<section class="page-head">
    <div>
        <h1>Actions</h1>
        <p class="muted">MCP tool calls waiting for approval and recent decisions.</p>
    </div>
</section>

<table class="ops-table">
    <thead>
        <tr><th>Status</th><th>Decision</th><th>Source</th><th>Tool</th><th>Created</th><th></th></tr>
    </thead>
    <tbody>
    {% for action in actions %}
        <tr>
            <td><span class="badge badge-{{ 'warn' if action.status == 'pending' else 'ok' if action.status == 'completed' else 'err' }}">{{ action.status }}</span></td>
            <td>{{ action.decision or "n/a" }}</td>
            <td>{{ action.channel_kind }} / {{ action.channel_ref }}</td>
            <td>{{ action.server_name }}.{{ action.tool_name }}</td>
            <td class="muted">{{ action.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
            <td><a href="/actions/{{ action.id }}">View</a></td>
        </tr>
    {% endfor %}
    </tbody>
</table>
{% if not actions %}
<p class="muted">No actions yet.</p>
{% endif %}
{% endblock %}
```

Create `jarvis/web/templates/action_detail.html`:

```html
{% extends "base.html" %}
{% block title %}Action {{ action.id }}{% endblock %}
{% block content %}
<section class="page-head">
    <div>
        <h1>{{ action.server_name }}.{{ action.tool_name }}</h1>
        <p class="muted">{{ action.channel_kind }} / {{ action.channel_ref }}</p>
    </div>
    <span class="badge badge-{{ 'warn' if action.status == 'pending' else 'ok' if action.status == 'completed' else 'err' }}">{{ action.status }}</span>
</section>

<section class="section-block">
<h2>Arguments</h2>
<pre>{{ arguments_pretty }}</pre>
</section>

<section class="section-block">
<h2>Decision</h2>
<table class="status-table">
    <tr><th>Decision</th><td>{{ action.decision or "n/a" }}</td></tr>
    <tr><th>Reason</th><td>{{ action.decision_reason or "n/a" }}</td></tr>
    <tr><th>Error</th><td>{{ action.error or "n/a" }}</td></tr>
</table>

{% if action.status == "pending" %}
<form method="post" action="/actions/{{ action.id }}/approve" class="inline-form">
    <button type="submit">Approve</button>
</form>
<form method="post" action="/actions/{{ action.id }}/reject" class="stack-form">
    <textarea name="reason" rows="3" placeholder="Reason for rejection"></textarea>
    <button type="submit">Reject</button>
</form>
{% endif %}
</section>
{% endblock %}
```

In `jarvis/web/templates/base.html`, add:

```html
<a href="/actions">Actions</a>
```

between Schedules and MCP.

In `jarvis/web/app.py`, include:

```python
from jarvis.web.routes.actions import router as actions_router

app.include_router(actions_router)
```

after schedules and before MCP.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/integration/test_web_actions.py tests/integration/test_web_mcp.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/routes/actions.py jarvis/web/templates/actions.html jarvis/web/templates/action_detail.html jarvis/web/templates/base.html jarvis/web/app.py tests/integration/test_web_actions.py
git commit -m "feat: add action inbox dashboard"
```

## Task 6: End-to-End Approval Behavior

**Files:**
- Modify: `tests/integration/test_agent_runner_actions.py`
- Modify: `tests/integration/test_action_service.py`
- Modify: `README.md`

- [ ] **Step 1: Add integration coverage for allowed and denied tools**

Extend `tests/integration/test_mcp_approval_policy.py` with:

```python
async def test_allow_override_skips_approval(factory):
    await _seed(factory, override="allow")
    policy = MCPApprovalPolicy(session_factory=factory)
    tool = SimpleNamespace(name="send_email")
    assert await policy.needs_approval("gmail", tool) is False
    assert await policy.filter_tool("gmail", tool) is True
```

Run:

```bash
uv run pytest tests/integration/test_mcp_approval_policy.py -q
```

Expected: passes.

- [ ] **Step 2: Add README dashboard note**

In `README.md`, under **Dashboard**, add:

```markdown
- **Actions** — approve or reject MCP tool calls that require confirmation before execution
```

Under **MCP Servers**, add:

```markdown
MCP tools can be marked `allow`, `confirm`, or `deny` from the dashboard. Read-like tools
auto-run by default; side-effecting tools pause in the Action Inbox until approved.
```

- [ ] **Step 3: Run full verification**

Run:

```bash
uv run ruff check jarvis tests
uv run pytest -q
```

Expected: both commands exit 0.

- [ ] **Step 4: Review final diff**

Run:

```bash
git diff --stat main..HEAD
git diff --check main..HEAD
```

Expected: scoped changes only; no whitespace errors.

- [ ] **Step 5: Commit final docs/test adjustments**

```bash
git add README.md tests/integration/test_mcp_approval_policy.py
git commit -m "docs: document action inbox"
```

## Final Verification Before PR

- [ ] `uv run ruff check jarvis tests`
- [ ] `uv run pytest -q`
- [ ] `git log --oneline main..HEAD`
- [ ] `git status --short`
- [ ] If a local server is started for browser QA, verify `/actions`, `/mcp`, and `/` render without template errors.

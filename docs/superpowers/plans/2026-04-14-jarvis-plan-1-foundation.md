# Jarvis Plan 1 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation of Jarvis — project scaffold, persistence layer (SQLAlchemy + Alembic), repositories, audit logger, config loader. At the end of this plan the codebase is a library with passing tests; no runtime yet (that's Plan 2).

**Architecture:** Python 3.12 project managed with `uv`. Async-throughout (SQLAlchemy `asyncio` + `aiosqlite`). Pydantic for all in-memory data types; SQLAlchemy ORM for persistence with repositories as the only way core modules touch the DB. YAML config via Pydantic validation with env-var expansion and hot-reload watcher.

**Tech Stack:** Python 3.12, `uv`, `pydantic>=2`, `sqlalchemy[asyncio]>=2`, `aiosqlite`, `alembic`, `pyyaml`, `watchfiles`, `pytest`, `pytest-asyncio`, `ruff`.

**Design spec this plan implements:** `docs/superpowers/specs/2026-04-14-jarvis-agent-service-design.md` (sections covered: §4.3 module layout, §5.8 AuditLogger, §7 data model, §10 config layout).

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `ruff.toml`
- Create: `pytest.ini`
- Modify: `.gitignore`
- Create: `jarvis/__init__.py`
- Create: `jarvis/core/__init__.py`
- Create: `jarvis/channels/__init__.py`
- Create: `jarvis/mcp/__init__.py`
- Create: `jarvis/scheduler/__init__.py`
- Create: `jarvis/persistence/__init__.py`
- Create: `jarvis/audit/__init__.py`
- Create: `jarvis/web/__init__.py`
- Create: `jarvis/config/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `.python-version`**

```
3.12
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "jarvis"
version = "0.1.0"
description = "Personal AI agent service"
requires-python = ">=3.12,<3.13"
dependencies = [
  "pydantic>=2.7",
  "sqlalchemy[asyncio]>=2.0.30",
  "aiosqlite>=0.20",
  "alembic>=1.13",
  "pyyaml>=6.0",
  "watchfiles>=0.22",
]

[dependency-groups]
dev = [
  "pytest>=8.2",
  "pytest-asyncio>=0.23",
  "ruff>=0.5",
  "freezegun>=1.5",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["jarvis"]
```

- [ ] **Step 3: Write `ruff.toml`**

```toml
line-length = 100
target-version = "py312"

[lint]
select = ["E", "F", "W", "I", "B", "UP", "ASYNC", "RUF"]
ignore = ["E501"]  # line length handled by formatter

[format]
quote-style = "double"
```

- [ ] **Step 4: Write `pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
addopts = -q --tb=short
```

- [ ] **Step 5: Extend `.gitignore`**

Append these lines to the existing `.gitignore`:

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
build/
dist/

# Test / tooling caches
.pytest_cache/
.ruff_cache/
.mypy_cache/

# Jarvis runtime
data/
*.db
*.db-wal
*.db-shm
.env
!.env.example
```

- [ ] **Step 6: Create empty `__init__.py` files**

Run:

```bash
touch jarvis/__init__.py \
      jarvis/core/__init__.py \
      jarvis/channels/__init__.py \
      jarvis/mcp/__init__.py \
      jarvis/scheduler/__init__.py \
      jarvis/persistence/__init__.py \
      jarvis/audit/__init__.py \
      jarvis/web/__init__.py \
      jarvis/config/__init__.py \
      tests/__init__.py \
      tests/unit/__init__.py \
      tests/integration/__init__.py
```

- [ ] **Step 7: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures."""
```

- [ ] **Step 8: Install dependencies**

Run: `uv sync`
Expected: `.venv/` created, deps resolved.

- [ ] **Step 9: Verify pytest runs (no tests yet)**

Run: `uv run pytest`
Expected: `no tests ran in X.XXs` (exit code 5 is OK — treat as success for this step).

- [ ] **Step 10: Verify ruff runs**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml .python-version ruff.toml pytest.ini .gitignore jarvis/ tests/
git commit -m "scaffold jarvis Python project with uv, ruff, pytest"
```

---

## Task 2: Core Pydantic types

Spec references: §4 `InvocationRequest`, §5.8 `AuditEvent`, §6 trigger kinds, §7 `audit_events.type` enum.

**Files:**
- Create: `jarvis/core/types.py`
- Create: `tests/unit/test_core_types.py`

- [ ] **Step 1: Write failing tests**

Write `tests/unit/test_core_types.py`:

```python
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from jarvis.core.types import (
    AuditEvent,
    AuditEventType,
    ChannelKind,
    ChannelMessage,
    InvocationRequest,
    ManualTrigger,
    ScheduledTrigger,
    TriggerKind,
)


def test_audit_event_type_values():
    # Must at least include these canonical types from §7 of the spec.
    required = {
        "trigger.received",
        "schedule.fired",
        "llm.request",
        "llm.response",
        "llm.error",
        "tool.call",
        "tool.result",
        "tool.error",
        "channel.sent",
        "output.suppressed",
        "config.reload_failed",
    }
    assert required.issubset({t.value for t in AuditEventType})


def test_audit_event_required_fields():
    ev = AuditEvent(
        type=AuditEventType.TRIGGER_RECEIVED,
        payload={"x": 1},
    )
    assert isinstance(ev.id, UUID)
    assert ev.created_at.tzinfo is timezone.utc
    assert ev.conversation_id is None
    assert ev.trigger_id is None


def test_channel_message_fields():
    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-123",
        text="hello",
        external_id="discord-msg-1",
    )
    assert msg.channel_kind == ChannelKind.DISCORD
    assert msg.text == "hello"


def test_scheduled_trigger_requires_prompt():
    with pytest.raises(ValidationError):
        ScheduledTrigger(schedule_id="s1", output_mode="discord")  # type: ignore[call-arg]


def test_invocation_request_accepts_all_trigger_kinds():
    for t in [
        ChannelMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="u1",
            text="hi",
            external_id="m1",
        ),
        ScheduledTrigger(schedule_id="s1", prompt="summarize", output_mode="discord"),
        ManualTrigger(user="mark", prompt="run it"),
    ]:
        req = InvocationRequest(trigger=t)
        assert req.trigger.kind in {k.value for k in TriggerKind}


def test_invocation_request_has_uuid_and_time():
    req = InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hi"))
    assert isinstance(req.id, UUID)
    assert req.created_at.tzinfo is timezone.utc
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/unit/test_core_types.py -v`
Expected: `ModuleNotFoundError` on `jarvis.core.types`.

- [ ] **Step 3: Write `jarvis/core/types.py`**

```python
"""Core in-memory types shared across jarvis modules."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChannelKind(str, Enum):
    DISCORD = "discord"
    SCHEDULED = "scheduled"
    DASHBOARD = "dashboard"


class TriggerKind(str, Enum):
    DISCORD_MESSAGE = "discord_message"
    SCHEDULE = "schedule"
    MANUAL = "manual"


class AuditEventType(str, Enum):
    TRIGGER_RECEIVED = "trigger.received"
    SCHEDULE_FIRED = "schedule.fired"
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    LLM_ERROR = "llm.error"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"
    CHANNEL_SENT = "channel.sent"
    OUTPUT_SUPPRESSED = "output.suppressed"
    CONFIG_RELOAD_FAILED = "config.reload_failed"
    MCP_CONNECTED = "mcp.connected"
    MCP_DISCONNECTED = "mcp.disconnected"


class _ModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditEvent(_ModelBase):
    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID | None = None
    trigger_id: UUID | None = None
    type: AuditEventType
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class ChannelMessage(_ModelBase):
    kind: Literal[TriggerKind.DISCORD_MESSAGE] = TriggerKind.DISCORD_MESSAGE
    channel_kind: ChannelKind
    channel_ref: str
    text: str
    external_id: str  # platform-native message id (for dedup)


class ScheduledTrigger(_ModelBase):
    kind: Literal[TriggerKind.SCHEDULE] = TriggerKind.SCHEDULE
    schedule_id: str
    prompt: str
    output_mode: Literal["discord", "dashboard_only", "discord_if_noteworthy"]


class ManualTrigger(_ModelBase):
    kind: Literal[TriggerKind.MANUAL] = TriggerKind.MANUAL
    user: str
    prompt: str


Trigger = Annotated[
    Union[ChannelMessage, ScheduledTrigger, ManualTrigger],
    Field(discriminator="kind"),
]


class InvocationRequest(_ModelBase):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_utcnow)
    trigger: Trigger
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/unit/test_core_types.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/core/types.py tests/unit/test_core_types.py
git commit -m "add core Pydantic types: AuditEvent, InvocationRequest, triggers"
```

---

## Task 3: Database engine + session factory

Spec references: §7 SQLite WAL mode, §10 `./data/jarvis.db` path.

**Files:**
- Create: `jarvis/persistence/db.py`
- Create: `tests/unit/test_db.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/test_db.py`:

```python
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from jarvis.persistence.db import Base, create_engine, session_factory


async def test_create_engine_returns_async_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()


async def test_session_factory_yields_async_session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    factory = session_factory(engine)
    async with factory() as session:
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("select 1"))
        assert result.scalar() == 1
    await engine.dispose()


async def test_base_has_metadata():
    assert Base.metadata is not None
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/unit/test_db.py -v`
Expected: `ModuleNotFoundError` on `jarvis.persistence.db`.

- [ ] **Step 3: Write `jarvis/persistence/db.py`**

```python
"""SQLAlchemy async engine, session factory, and declarative Base."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an async engine. SQLite gets WAL mode via pragma on connect."""
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_async_engine(
        url,
        echo=echo,
        connect_args=connect_args,
        future=True,
    )

    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def session_factory(engine: AsyncEngine) -> Callable[[], AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/unit/test_db.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/db.py tests/unit/test_db.py
git commit -m "add async SQLAlchemy engine and session factory with SQLite WAL"
```

---

## Task 4: ORM models — conversation-flow tables

Spec references: §7 tables `conversations`, `messages`, `triggers`, `audit_events`.

**Files:**
- Create: `jarvis/persistence/models.py`
- Create: `tests/integration/test_orm_flow_tables.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_orm_flow_tables.py`:

```python
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import (
    AuditEventRow,
    ConversationRow,
    MessageRow,
    TriggerRow,
)


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_conversation_roundtrip(session):
    conv = ConversationRow(
        id=uuid4(),
        channel_kind="discord",
        channel_ref="user-1",
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        status="open",
    )
    session.add(conv)
    await session.commit()

    result = await session.execute(select(ConversationRow))
    found = result.scalar_one()
    assert found.channel_ref == "user-1"
    assert found.status == "open"


async def test_message_belongs_to_conversation(session):
    conv = ConversationRow(
        id=uuid4(),
        channel_kind="discord",
        channel_ref="user-1",
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        status="open",
    )
    session.add(conv)
    await session.flush()

    msg = MessageRow(
        id=uuid4(),
        conversation_id=conv.id,
        role="user",
        content="hi",
        created_at=datetime.now(timezone.utc),
    )
    session.add(msg)
    await session.commit()

    result = await session.execute(select(MessageRow))
    found = result.scalar_one()
    assert found.content == "hi"
    assert found.conversation_id == conv.id


async def test_audit_event_allows_null_conversation(session):
    ev = AuditEventRow(
        id=uuid4(),
        conversation_id=None,
        trigger_id=None,
        type="config.reload_failed",
        payload={"error": "bad yaml"},
        created_at=datetime.now(timezone.utc),
    )
    session.add(ev)
    await session.commit()

    result = await session.execute(select(AuditEventRow))
    assert result.scalar_one().type == "config.reload_failed"


async def test_trigger_roundtrip(session):
    trig = TriggerRow(
        id=uuid4(),
        kind="discord_message",
        source_ref="discord-msg-abc",
        created_at=datetime.now(timezone.utc),
    )
    session.add(trig)
    await session.commit()

    result = await session.execute(select(TriggerRow))
    assert result.scalar_one().source_ref == "discord-msg-abc"
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/integration/test_orm_flow_tables.py -v`
Expected: `ModuleNotFoundError` on `jarvis.persistence.models`.

- [ ] **Step 3: Write `jarvis/persistence/models.py` (flow tables only for now)**

```python
"""SQLAlchemy ORM models. Column names match the design doc's data model."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jarvis.persistence.db import Base


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    channel_kind: Mapped[str] = mapped_column(String(32))
    channel_ref: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="open")
    idle_timeout_sec: Mapped[int | None] = mapped_column(default=None)

    messages: Mapped[list["MessageRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))  # 'user' | 'assistant' | 'system'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[ConversationRow] = relationship(back_populates="messages")


class TriggerRow(Base):
    __tablename__ = "triggers"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    trigger_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/integration/test_orm_flow_tables.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/models.py tests/integration/test_orm_flow_tables.py
git commit -m "add ORM models for conversations, messages, triggers, audit_events"
```

---

## Task 5: ORM models — domain tables (schedules, MCP, settings)

Spec references: §7 tables `schedules`, `mcp_servers`, `mcp_tools`, `settings`.

**Files:**
- Modify: `jarvis/persistence/models.py` (append)
- Create: `tests/integration/test_orm_domain_tables.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_orm_domain_tables.py`:

```python
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import (
    MCPServerRow,
    MCPToolRow,
    ScheduleRow,
    SettingRow,
)


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_schedule_roundtrip(session):
    sched = ScheduleRow(
        id=uuid4(),
        name="morning email",
        description="summarize overnight email",
        cron_expr="0 8 * * *",
        timezone="America/Los_Angeles",
        prompt="summarize my unread email",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(sched)
    await session.commit()

    result = await session.execute(select(ScheduleRow))
    found = result.scalar_one()
    assert found.cron_expr == "0 8 * * *"
    assert found.last_run_at is None


async def test_mcp_server_and_tool(session):
    server = MCPServerRow(
        id=uuid4(),
        name="gcal",
        transport="stdio",
        status="disconnected",
    )
    session.add(server)
    await session.flush()

    tool = MCPToolRow(
        id=uuid4(),
        server_id=server.id,
        name="list_events",
        description="List calendar events",
        input_schema={"type": "object", "properties": {}},
        read_only_hint=True,
        destructive_hint=False,
        policy_override=None,
    )
    session.add(tool)
    await session.commit()

    result = await session.execute(select(MCPToolRow))
    found = result.scalar_one()
    assert found.name == "list_events"
    assert found.read_only_hint is True


async def test_setting_key_value(session):
    s = SettingRow(key="idle_timeout_sec", value=1800)
    session.add(s)
    await session.commit()

    result = await session.execute(select(SettingRow))
    assert result.scalar_one().value == 1800
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/integration/test_orm_domain_tables.py -v`
Expected: `ImportError` — the new classes don't exist yet.

- [ ] **Step 3: Append to `jarvis/persistence/models.py`**

Add at the end of the file:

```python


class ScheduleRow(Base):
    __tablename__ = "schedules"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    cron_expr: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    output_mode: Mapped[str] = mapped_column(String(32))
    notify_on_error: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)


class MCPServerRow(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    transport: Mapped[str] = mapped_column(String(16))  # 'stdio' | 'http' | 'sse'
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tools: Mapped[list["MCPToolRow"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class MCPToolRow(Base):
    __tablename__ = "mcp_tools"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    read_only_hint: Mapped[bool | None] = mapped_column(nullable=True)
    destructive_hint: Mapped[bool | None] = mapped_column(nullable=True)
    policy_override: Mapped[str | None] = mapped_column(String(16), nullable=True)

    server: Mapped[MCPServerRow] = relationship(back_populates="tools")


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/integration/test_orm_domain_tables.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run full test suite to confirm nothing broke**

Run: `uv run pytest`
Expected: all tests pass (flow + domain + earlier).

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/models.py tests/integration/test_orm_domain_tables.py
git commit -m "add ORM models for schedules, mcp_servers, mcp_tools, settings"
```

---

## Task 6: Alembic setup + initial migration

Spec references: §7 "Alembic migrations from day one."

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/` (directory, kept with `.gitkeep`)
- Create: `alembic/versions/0001_initial.py` (auto-generated, then committed)
- Create: `tests/integration/test_migrations.py`

- [ ] **Step 1: Initialize Alembic**

Run: `uv run alembic init -t async alembic`
Expected: `alembic/` directory created with template files, `alembic.ini` created in repo root.

- [ ] **Step 2: Edit `alembic.ini`**

Find `sqlalchemy.url = driver://user:pass@localhost/dbname` and replace with:

```ini
sqlalchemy.url = sqlite+aiosqlite:///data/jarvis.db
```

- [ ] **Step 3: Replace `alembic/env.py`**

Overwrite the generated `alembic/env.py` with:

```python
"""Alembic environment — async, reads metadata from jarvis.persistence.models."""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import Base so MetaData has all models registered.
from jarvis.persistence import models  # noqa: F401  (registers tables)
from jarvis.persistence.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 4: Auto-generate initial migration**

Run: `mkdir -p data && uv run alembic revision --autogenerate -m "initial schema"`
Expected: new file in `alembic/versions/` with a name like `xxxxxx_initial_schema.py`.

- [ ] **Step 5: Rename migration for stable ordering**

Run (replace the `xxxxxx_initial_schema.py` filename with whatever Alembic generated):

```bash
mv alembic/versions/*_initial_schema.py alembic/versions/0001_initial_schema.py
```

Open the renamed file and change the `revision` line to:

```python
revision: str = "0001"
```

- [ ] **Step 6: Write migration test**

Write `tests/integration/test_migrations.py`:

```python
import subprocess
from pathlib import Path


def test_migration_applies_cleanly(tmp_path):
    """Run alembic upgrade head against a throwaway sqlite db."""
    db_path = tmp_path / "test.db"
    env = {
        "PATH": __import__("os").environ["PATH"],
        "SQLALCHEMY_URL": f"sqlite+aiosqlite:///{db_path}",
    }
    # Use -x to override the ini url without editing the file.
    result = subprocess.run(
        [
            "uv", "run", "alembic",
            "-x", f"db_url=sqlite+aiosqlite:///{db_path}",
            "upgrade", "head",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr
    assert db_path.exists()


def test_migration_roundtrip(tmp_path):
    """upgrade head then downgrade base should both succeed."""
    db_path = tmp_path / "test.db"
    cwd = Path(__file__).resolve().parents[2]
    for cmd in (
        ["uv", "run", "alembic", "-x", f"db_url=sqlite+aiosqlite:///{db_path}",
         "upgrade", "head"],
        ["uv", "run", "alembic", "-x", f"db_url=sqlite+aiosqlite:///{db_path}",
         "downgrade", "base"],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        assert r.returncode == 0, r.stderr
```

- [ ] **Step 7: Teach env.py to honor the `-x db_url=` override**

In `alembic/env.py`, after the line `config = context.config`, add:

```python
# Allow tests / runtime to override the url via `-x db_url=...`
_x_args = context.get_x_argument(as_dictionary=True)
if "db_url" in _x_args:
    config.set_main_option("sqlalchemy.url", _x_args["db_url"])
```

- [ ] **Step 8: Run migration tests**

Run: `uv run pytest tests/integration/test_migrations.py -v`
Expected: 2 passed.

- [ ] **Step 9: Commit**

```bash
git add alembic.ini alembic/ tests/integration/test_migrations.py
git commit -m "add alembic with initial autogenerated migration"
```

---

## Task 7: Repositories — ConversationRepo, MessageRepo, TriggerRepo

Spec references: §4.4 "Core components use repositories, not ORM models." §5.2 "Conversation lookup/creation honoring idle timeout."

**Files:**
- Create: `jarvis/persistence/repositories.py`
- Create: `tests/integration/test_repositories_flow.py`

- [ ] **Step 1: Write failing tests**

Write `tests/integration/test_repositories_flow.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.core.types import ChannelKind
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    ConversationRepo,
    MessageRepo,
    TriggerRepo,
)


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_conversation_find_or_create_creates_new(session):
    repo = ConversationRepo(session)
    conv = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    assert conv.status == "open"


async def test_conversation_find_or_create_returns_existing_if_active(session):
    repo = ConversationRepo(session)
    c1 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    c2 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    assert c1.id == c2.id


async def test_conversation_find_or_create_opens_new_after_idle_timeout(session):
    repo = ConversationRepo(session)
    c1 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    # Force c1 to look stale.
    c1.last_activity_at = datetime.now(timezone.utc) - timedelta(seconds=1000)
    await session.commit()

    c2 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    assert c1.id != c2.id
    await session.refresh(c1)
    assert c1.status == "closed"


async def test_scheduled_trigger_always_fresh_conversation(session):
    repo = ConversationRepo(session)
    c1 = await repo.find_or_create_open(
        channel_kind=ChannelKind.SCHEDULED,
        channel_ref="schedule-abc",
        idle_timeout_sec=0,  # 0 means: always fresh
    )
    c2 = await repo.find_or_create_open(
        channel_kind=ChannelKind.SCHEDULED,
        channel_ref="schedule-abc",
        idle_timeout_sec=0,
    )
    assert c1.id != c2.id


async def test_message_repo_appends(session):
    conv_repo = ConversationRepo(session)
    conv = await conv_repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )

    msg_repo = MessageRepo(session)
    await msg_repo.append(conversation_id=conv.id, role="user", content="hello")
    await msg_repo.append(conversation_id=conv.id, role="assistant", content="hi there")

    history = await msg_repo.history(conv.id)
    assert [m.role for m in history] == ["user", "assistant"]
    assert [m.content for m in history] == ["hello", "hi there"]


async def test_trigger_repo_records(session):
    repo = TriggerRepo(session)
    trig = await repo.record(kind="discord_message", source_ref="discord-msg-abc")
    assert trig.kind == "discord_message"
    assert trig.source_ref == "discord-msg-abc"
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/integration/test_repositories_flow.py -v`
Expected: `ImportError` on `jarvis.persistence.repositories`.

- [ ] **Step 3: Write `jarvis/persistence/repositories.py`**

```python
"""Repositories — the only way core modules touch the database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.types import ChannelKind
from jarvis.persistence.models import (
    ConversationRow,
    MessageRow,
    TriggerRow,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConversationRepo:
    """Per-channel conversation sessions with idle-timeout semantics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_or_create_open(
        self,
        *,
        channel_kind: ChannelKind,
        channel_ref: str,
        idle_timeout_sec: int,
    ) -> ConversationRow:
        """Return an open conversation for (kind, ref). If the newest open one is
        stale (last_activity older than idle_timeout_sec), close it and open a
        fresh one. An idle_timeout_sec of 0 always opens a fresh conversation.
        """
        now = _utcnow()

        if idle_timeout_sec == 0:
            return await self._create(channel_kind, channel_ref, now)

        result = await self._session.execute(
            select(ConversationRow)
            .where(
                ConversationRow.channel_kind == channel_kind.value,
                ConversationRow.channel_ref == channel_ref,
                ConversationRow.status == "open",
            )
            .order_by(ConversationRow.last_activity_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            threshold = now - timedelta(seconds=idle_timeout_sec)
            if existing.last_activity_at >= threshold:
                existing.last_activity_at = now
                await self._session.commit()
                await self._session.refresh(existing)
                return existing
            existing.status = "closed"
            await self._session.commit()

        return await self._create(channel_kind, channel_ref, now)

    async def _create(
        self,
        channel_kind: ChannelKind,
        channel_ref: str,
        now: datetime,
    ) -> ConversationRow:
        conv = ConversationRow(
            channel_kind=channel_kind.value,
            channel_ref=channel_ref,
            started_at=now,
            last_activity_at=now,
            status="open",
        )
        self._session.add(conv)
        await self._session.commit()
        await self._session.refresh(conv)
        return conv

    async def touch(self, conversation_id: UUID) -> None:
        await self._session.execute(
            update(ConversationRow)
            .where(ConversationRow.id == conversation_id)
            .values(last_activity_at=_utcnow())
        )
        await self._session.commit()


class MessageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        conversation_id: UUID,
        role: str,
        content: str,
    ) -> MessageRow:
        msg = MessageRow(
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=_utcnow(),
        )
        self._session.add(msg)
        await self._session.commit()
        await self._session.refresh(msg)
        return msg

    async def history(self, conversation_id: UUID) -> list[MessageRow]:
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.created_at.asc())
        )
        return list(result.scalars())


class TriggerRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, *, kind: str, source_ref: str) -> TriggerRow:
        trig = TriggerRow(kind=kind, source_ref=source_ref, created_at=_utcnow())
        self._session.add(trig)
        await self._session.commit()
        await self._session.refresh(trig)
        return trig
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/integration/test_repositories_flow.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_flow.py
git commit -m "add ConversationRepo, MessageRepo, TriggerRepo with idle-timeout semantics"
```

---

## Task 8: Repositories — AuditRepo, ScheduleRepo

Spec references: §5.8 AuditLogger writes via this repo; §5.7 Scheduler loads enabled schedules on startup.

**Files:**
- Modify: `jarvis/persistence/repositories.py` (append)
- Create: `tests/integration/test_repositories_audit_schedule.py`

- [ ] **Step 1: Write failing tests**

Write `tests/integration/test_repositories_audit_schedule.py`:

```python
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo, ScheduleRepo


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_audit_write_and_query_by_type(session):
    repo = AuditRepo(session)
    await repo.write_many([
        AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"x": 1}),
        AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"y": 2}),
        AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"x": 3}),
    ])
    rows = await repo.recent(types=[AuditEventType.TRIGGER_RECEIVED], limit=10)
    assert len(rows) == 2


async def test_audit_recent_respects_limit_and_order(session):
    repo = AuditRepo(session)
    events = [
        AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"i": i})
        for i in range(5)
    ]
    await repo.write_many(events)
    rows = await repo.recent(limit=3)
    assert len(rows) == 3
    # Newest first
    assert rows[0].created_at >= rows[-1].created_at


async def test_schedule_crud(session):
    repo = ScheduleRepo(session)
    created = await repo.create(
        name="morning",
        description="test",
        cron_expr="0 8 * * *",
        timezone="UTC",
        prompt="summarize",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
    )
    assert created.id is not None

    found = await repo.get(created.id)
    assert found is not None
    assert found.name == "morning"

    all_enabled = await repo.list_enabled()
    assert len(all_enabled) == 1

    await repo.set_enabled(created.id, False)
    all_enabled = await repo.list_enabled()
    assert len(all_enabled) == 0


async def test_schedule_record_run(session):
    repo = ScheduleRepo(session)
    sched = await repo.create(
        name="s", description="", cron_expr="* * * * *",
        timezone="UTC", prompt="go", output_mode="discord",
        notify_on_error=True, enabled=True,
    )
    ts = datetime.now(timezone.utc)
    await repo.record_run(sched.id, at=ts, status="success")
    refreshed = await repo.get(sched.id)
    assert refreshed.last_run_status == "success"
    assert refreshed.last_run_at == ts
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py -v`
Expected: `ImportError` on `AuditRepo` / `ScheduleRepo`.

- [ ] **Step 3: Append to `jarvis/persistence/repositories.py`**

Add to the imports at the top of the file:

```python
from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.models import AuditEventRow, ScheduleRow
```

Then append to the file:

```python


class AuditRepo:
    """Append-only audit event store."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def write_many(self, events: list[AuditEvent]) -> None:
        rows = [
            AuditEventRow(
                id=e.id,
                conversation_id=e.conversation_id,
                trigger_id=e.trigger_id,
                type=e.type.value,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ]
        self._session.add_all(rows)
        await self._session.commit()

    async def recent(
        self,
        *,
        types: list[AuditEventType] | None = None,
        limit: int = 100,
    ) -> list[AuditEventRow]:
        stmt = select(AuditEventRow).order_by(AuditEventRow.created_at.desc()).limit(limit)
        if types:
            stmt = stmt.where(AuditEventRow.type.in_([t.value for t in types]))
        result = await self._session.execute(stmt)
        return list(result.scalars())


class ScheduleRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        name: str,
        description: str,
        cron_expr: str,
        timezone: str,
        prompt: str,
        output_mode: str,
        notify_on_error: bool,
        enabled: bool,
    ) -> ScheduleRow:
        now = _utcnow()
        row = ScheduleRow(
            name=name,
            description=description,
            cron_expr=cron_expr,
            timezone=timezone,
            prompt=prompt,
            output_mode=output_mode,
            notify_on_error=notify_on_error,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get(self, schedule_id: UUID) -> ScheduleRow | None:
        return await self._session.get(ScheduleRow, schedule_id)

    async def list_enabled(self) -> list[ScheduleRow]:
        result = await self._session.execute(
            select(ScheduleRow).where(ScheduleRow.enabled.is_(True))
        )
        return list(result.scalars())

    async def set_enabled(self, schedule_id: UUID, enabled: bool) -> None:
        await self._session.execute(
            update(ScheduleRow)
            .where(ScheduleRow.id == schedule_id)
            .values(enabled=enabled, updated_at=_utcnow())
        )
        await self._session.commit()

    async def record_run(
        self,
        schedule_id: UUID,
        *,
        at: datetime,
        status: str,
    ) -> None:
        await self._session.execute(
            update(ScheduleRow)
            .where(ScheduleRow.id == schedule_id)
            .values(last_run_at=at, last_run_status=status)
        )
        await self._session.commit()
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_audit_schedule.py
git commit -m "add AuditRepo and ScheduleRepo"
```

---

## Task 9: Repositories — MCPServerRepo, MCPToolRepo, SettingsRepo

Spec references: §5.5 MCPManager uses these to shadow YAML → DB for dashboard.

**Files:**
- Modify: `jarvis/persistence/repositories.py` (append)
- Create: `tests/integration/test_repositories_mcp_settings.py`

- [ ] **Step 1: Write failing tests**

Write `tests/integration/test_repositories_mcp_settings.py`:

```python
import pytest

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    MCPServerRepo,
    MCPToolRepo,
    SettingsRepo,
)


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_mcp_server_upsert_by_name(session):
    repo = MCPServerRepo(session)
    s1 = await repo.upsert(name="gcal", transport="stdio")
    s2 = await repo.upsert(name="gcal", transport="stdio")
    assert s1.id == s2.id


async def test_mcp_server_status_update(session):
    repo = MCPServerRepo(session)
    s = await repo.upsert(name="gcal", transport="stdio")
    await repo.set_status(s.id, status="connected", last_error=None)

    listed = await repo.list_all()
    assert listed[0].status == "connected"


async def test_mcp_tool_replace_for_server(session):
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    server = await srepo.upsert(name="gcal", transport="stdio")

    await trepo.replace_for_server(
        server.id,
        tools=[
            {
                "name": "list_events",
                "description": "",
                "input_schema": {},
                "read_only_hint": True,
                "destructive_hint": False,
            },
            {
                "name": "create_event",
                "description": "",
                "input_schema": {},
                "read_only_hint": False,
                "destructive_hint": False,
            },
        ],
    )
    got = await trepo.list_for_server(server.id)
    assert {t.name for t in got} == {"list_events", "create_event"}

    # Replacing with a smaller set removes the old rows.
    await trepo.replace_for_server(
        server.id,
        tools=[
            {"name": "list_events", "description": "", "input_schema": {},
             "read_only_hint": True, "destructive_hint": False},
        ],
    )
    got = await trepo.list_for_server(server.id)
    assert {t.name for t in got} == {"list_events"}


async def test_settings_get_set(session):
    repo = SettingsRepo(session)
    assert await repo.get("missing") is None

    await repo.set("idle_timeout_sec", 1800)
    assert await repo.get("idle_timeout_sec") == 1800

    await repo.set("idle_timeout_sec", 600)
    assert await repo.get("idle_timeout_sec") == 600
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/integration/test_repositories_mcp_settings.py -v`
Expected: `ImportError` on `MCPServerRepo` / `MCPToolRepo` / `SettingsRepo`.

- [ ] **Step 3: Append to `jarvis/persistence/repositories.py`**

Add to the imports at the top:

```python
from jarvis.persistence.models import MCPServerRow, MCPToolRow, SettingRow
```

Append to the file:

```python


class MCPServerRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, *, name: str, transport: str) -> MCPServerRow:
        result = await self._session.execute(
            select(MCPServerRow).where(MCPServerRow.name == name)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.transport = transport
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        row = MCPServerRow(name=name, transport=transport, status="disconnected")
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def set_status(
        self,
        server_id: UUID,
        *,
        status: str,
        last_error: str | None,
    ) -> None:
        values: dict = {"status": status, "last_error": last_error}
        if status == "connected":
            values["last_connected_at"] = _utcnow()
        await self._session.execute(
            update(MCPServerRow).where(MCPServerRow.id == server_id).values(**values)
        )
        await self._session.commit()

    async def list_all(self) -> list[MCPServerRow]:
        result = await self._session.execute(select(MCPServerRow))
        return list(result.scalars())


class MCPToolRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_server(
        self,
        server_id: UUID,
        *,
        tools: list[dict],
    ) -> None:
        """Replace the tool set for a server atomically (full overwrite)."""
        # Delete existing rows for this server.
        existing = await self._session.execute(
            select(MCPToolRow).where(MCPToolRow.server_id == server_id)
        )
        for row in existing.scalars():
            await self._session.delete(row)

        for tool in tools:
            self._session.add(
                MCPToolRow(
                    server_id=server_id,
                    name=tool["name"],
                    description=tool.get("description", ""),
                    input_schema=tool.get("input_schema", {}),
                    read_only_hint=tool.get("read_only_hint"),
                    destructive_hint=tool.get("destructive_hint"),
                    policy_override=tool.get("policy_override"),
                )
            )
        await self._session.commit()

    async def list_for_server(self, server_id: UUID) -> list[MCPToolRow]:
        result = await self._session.execute(
            select(MCPToolRow).where(MCPToolRow.server_id == server_id)
        )
        return list(result.scalars())


class SettingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> object | None:
        row = await self._session.get(SettingRow, key)
        return row.value if row is not None else None

    async def set(self, key: str, value: object) -> None:
        existing = await self._session.get(SettingRow, key)
        if existing is None:
            self._session.add(SettingRow(key=key, value=value))
        else:
            existing.value = value
        await self._session.commit()
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/integration/test_repositories_mcp_settings.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_mcp_settings.py
git commit -m "add MCPServerRepo, MCPToolRepo, SettingsRepo"
```

---

## Task 10: AuditLogger (buffered async sink)

Spec references: §5.8 "Single sink. Async, buffered, flushed on a short interval."

**Files:**
- Create: `jarvis/audit/logger.py`
- Create: `tests/integration/test_audit_logger.py`

- [ ] **Step 1: Write failing tests**

Write `tests/integration/test_audit_logger.py`:

```python
import asyncio

import pytest

from jarvis.audit.logger import AuditLogger
from jarvis.core.types import AuditEvent, AuditEventType
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


async def test_emit_then_stop_persists_events(engine_and_factory):
    _, factory = engine_and_factory
    logger = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await logger.start()
    try:
        for i in range(3):
            await logger.emit(AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"i": i}))
    finally:
        await logger.stop()

    async with factory() as s:
        rows = await AuditRepo(s).recent(limit=10)
    assert len(rows) == 3


async def test_flush_interval_triggers_writes(engine_and_factory):
    _, factory = engine_and_factory
    logger = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await logger.start()
    try:
        await logger.emit(AuditEvent(type=AuditEventType.LLM_REQUEST))
        await asyncio.sleep(0.1)  # longer than flush_interval
        async with factory() as s:
            rows = await AuditRepo(s).recent(limit=10)
        assert len(rows) == 1
    finally:
        await logger.stop()


async def test_batch_size_flushes_full_buffer(engine_and_factory):
    _, factory = engine_and_factory
    logger = AuditLogger(
        session_factory=factory,
        flush_interval_sec=0.02,
        batch_size=3,  # small batch; drain cap per flush
    )
    await logger.start()
    try:
        for i in range(10):
            await logger.emit(AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"i": i}))
        # A few flush cycles at flush_interval 0.02 should drain all 10.
        await asyncio.sleep(0.2)
        async with factory() as s:
            rows = await AuditRepo(s).recent(limit=20)
        assert len(rows) == 10
    finally:
        await logger.stop()
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/integration/test_audit_logger.py -v`
Expected: `ModuleNotFoundError` on `jarvis.audit.logger`.

- [ ] **Step 3: Write `jarvis/audit/logger.py`**

```python
"""AuditLogger — buffered async sink that writes to AuditRepo.

One queue, one background flusher. On each tick the flusher drains up to
`batch_size` events from the queue (if any) and writes them. `stop()`
drains remaining events before returning so shutdown never loses events.

The logger owns session lifecycle: it opens a fresh session per flush via
the provided `session_factory` and closes it when the flush finishes.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.core.types import AuditEvent
from jarvis.persistence.repositories import AuditRepo

_log = logging.getLogger(__name__)


class AuditLogger:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        flush_interval_sec: float = 0.1,
        batch_size: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._flush_interval = flush_interval_sec
        self._batch_size = batch_size
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("AuditLogger already started")
        self._task = asyncio.create_task(self._run(), name="audit-logger")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        await self._task
        self._task = None

    async def emit(self, event: AuditEvent) -> None:
        if self._task is None:
            raise RuntimeError("AuditLogger not started")
        await self._queue.put(event)

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
                if self._stopping.is_set() and self._queue.empty():
                    return
            except Exception:  # noqa: BLE001 — top-level flusher must not die silently
                _log.exception("audit logger loop error")

    async def _tick(self) -> None:
        buffer: list[AuditEvent] = []
        # Wait up to flush_interval for at least one event (unless stopping).
        try:
            first = await asyncio.wait_for(
                self._queue.get(), timeout=self._flush_interval
            )
            buffer.append(first)
        except asyncio.TimeoutError:
            return

        # Opportunistically drain the queue up to batch_size.
        while not self._queue.empty() and len(buffer) < self._batch_size:
            buffer.append(self._queue.get_nowait())

        await self._flush(buffer)

    async def _flush(self, events: list[AuditEvent]) -> None:
        async with self._session_factory() as session:
            repo = AuditRepo(session)
            await repo.write_many(events)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/integration/test_audit_logger.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/audit/logger.py tests/integration/test_audit_logger.py
git commit -m "add buffered async AuditLogger"
```

---

## Task 11: Config schemas (Pydantic)

Spec references: §10 config layout — `jarvis.yaml`, `mcp-servers.yaml`, `channels.yaml`.

**Files:**
- Create: `jarvis/config/schema.py`
- Create: `tests/unit/test_config_schema.py`

- [ ] **Step 1: Write failing tests**

Write `tests/unit/test_config_schema.py`:

```python
import pytest
from pydantic import ValidationError

from jarvis.config.schema import (
    ChannelsConfig,
    DiscordChannelConfig,
    JarvisConfig,
    LLMConfig,
    MCPServerConfig,
    MCPServersConfig,
)


def test_jarvis_config_minimal():
    cfg = JarvisConfig(
        llm=LLMConfig(
            base_url="http://host.docker.internal:1234/v1",
            api_key="dummy",
            model="qwen2.5:32b",
        ),
    )
    assert cfg.idle_timeout_sec == 900  # default
    assert cfg.max_concurrent_agents == 3  # default
    assert cfg.timezone == "UTC"  # default


def test_jarvis_config_rejects_bad_output_fallback():
    with pytest.raises(ValidationError):
        JarvisConfig(
            llm=LLMConfig(base_url="x", api_key="x", model="x"),
            default_schedule_output_mode="pigeon",  # type: ignore[arg-type]
        )


def test_discord_channel_requires_token_and_allow_list():
    with pytest.raises(ValidationError):
        DiscordChannelConfig()  # type: ignore[call-arg]

    ok = DiscordChannelConfig(token="abc", allowed_user_ids=["1", "2"])
    assert ok.enabled is True
    assert len(ok.allowed_user_ids) == 2


def test_channels_config_discord_optional():
    cfg = ChannelsConfig()
    assert cfg.discord is None


def test_mcp_server_transport_validation():
    # stdio requires command
    with pytest.raises(ValidationError):
        MCPServerConfig(name="gcal", transport="stdio")  # type: ignore[call-arg]

    # http requires url
    with pytest.raises(ValidationError):
        MCPServerConfig(name="gcal", transport="http")  # type: ignore[call-arg]

    stdio_ok = MCPServerConfig(
        name="gcal",
        transport="stdio",
        command=["python", "-m", "mcp_server_gcal"],
    )
    assert stdio_ok.command[0] == "python"

    http_ok = MCPServerConfig(name="gcal", transport="http", url="http://x.local/mcp")
    assert http_ok.url == "http://x.local/mcp"


def test_mcp_servers_config_accepts_empty():
    cfg = MCPServersConfig(servers=[])
    assert cfg.servers == []
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/unit/test_config_schema.py -v`
Expected: `ModuleNotFoundError` on `jarvis.config.schema`.

- [ ] **Step 3: Write `jarvis/config/schema.py`**

```python
"""Pydantic schemas for YAML configs. Source of truth for file layout."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------
# jarvis.yaml
# --------------------------------------------------------------------

OutputMode = Literal["discord", "dashboard_only", "discord_if_noteworthy"]


class LLMConfig(_StrictModel):
    base_url: str
    api_key: str
    model: str
    request_timeout_sec: float = 60.0


class JarvisConfig(_StrictModel):
    llm: LLMConfig
    timezone: str = "UTC"
    idle_timeout_sec: int = 900
    max_concurrent_agents: int = 3
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    default_schedule_output_mode: OutputMode = "discord"


# --------------------------------------------------------------------
# channels.yaml
# --------------------------------------------------------------------


class DiscordChannelConfig(_StrictModel):
    token: str
    allowed_user_ids: list[str] = Field(min_length=1)
    enabled: bool = True


class ChannelsConfig(_StrictModel):
    discord: DiscordChannelConfig | None = None


# --------------------------------------------------------------------
# mcp-servers.yaml
# --------------------------------------------------------------------


class MCPServerConfig(_StrictModel):
    name: str
    transport: Literal["stdio", "http", "sse"]
    enabled: bool = True

    # stdio
    command: list[str] | None = None
    env: dict[str, str] | None = None

    # http / sse
    url: str | None = None
    headers: dict[str, str] | None = None

    @model_validator(mode="after")
    def _transport_fields_required(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires `command`")
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError(f"{self.transport} transport requires `url`")
        return self


class MCPServersConfig(_StrictModel):
    servers: list[MCPServerConfig] = Field(default_factory=list)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/unit/test_config_schema.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/config/schema.py tests/unit/test_config_schema.py
git commit -m "add Pydantic schemas for jarvis.yaml, channels.yaml, mcp-servers.yaml"
```

---

## Task 12: Config loader (YAML + env expansion)

Spec references: §10 "MCP server secrets pass through each server's own env in `mcp-servers.yaml` via `${VAR}` expansion."

**Files:**
- Create: `jarvis/config/loader.py`
- Create: `tests/unit/test_config_loader.py`

- [ ] **Step 1: Write failing tests**

Write `tests/unit/test_config_loader.py`:

```python
from pathlib import Path

import pytest

from jarvis.config.loader import ConfigLoadError, expand_env, load_config


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_expand_env_substitutes_vars(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    assert expand_env({"token": "${MY_TOKEN}"}) == {"token": "s3cret"}


def test_expand_env_nested(monkeypatch):
    monkeypatch.setenv("X", "v")
    out = expand_env({"a": {"b": ["${X}", "plain"]}})
    assert out == {"a": {"b": ["v", "plain"]}}


def test_expand_env_missing_var_raises(monkeypatch):
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(ConfigLoadError, match="MISSING"):
        expand_env({"k": "${MISSING}"})


def test_load_config_reads_all_three_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")

    _write(
        tmp_path / "jarvis.yaml",
        """
llm:
  base_url: http://host.docker.internal:1234/v1
  api_key: dummy
  model: qwen
""",
    )
    _write(
        tmp_path / "channels.yaml",
        """
discord:
  token: ${DISCORD_TOKEN}
  allowed_user_ids: ["111"]
""",
    )
    _write(
        tmp_path / "mcp-servers.yaml",
        """
servers:
  - name: gcal
    transport: stdio
    command: ["python", "-m", "mcp_server_gcal"]
""",
    )

    cfg = load_config(tmp_path)
    assert cfg.jarvis.llm.model == "qwen"
    assert cfg.channels.discord is not None
    assert cfg.channels.discord.token == "tok"
    assert cfg.mcp_servers.servers[0].name == "gcal"


def test_load_config_missing_required_file(tmp_path):
    with pytest.raises(ConfigLoadError, match="jarvis.yaml"):
        load_config(tmp_path)


def test_load_config_invalid_yaml(tmp_path):
    _write(tmp_path / "jarvis.yaml", "llm: [not valid")
    with pytest.raises(ConfigLoadError):
        load_config(tmp_path)
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/unit/test_config_loader.py -v`
Expected: `ModuleNotFoundError` on `jarvis.config.loader`.

- [ ] **Step 3: Write `jarvis/config/loader.py`**

```python
"""Load and validate YAML configs from a directory."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jarvis.config.schema import ChannelsConfig, JarvisConfig, MCPServersConfig

_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigLoadError(Exception):
    """Raised for any failure to load / validate config."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    jarvis: JarvisConfig
    channels: ChannelsConfig
    mcp_servers: MCPServersConfig


def expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} references. Missing vars raise ConfigLoadError."""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigLoadError(f"environment variable {name!r} is not set")
            return os.environ[name]

        return _ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def _load_yaml_file(path: Path) -> dict:
    if not path.exists():
        raise ConfigLoadError(f"required config file not found: {path.name}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigLoadError(f"{path.name}: YAML parse error: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigLoadError(f"{path.name}: top-level must be a mapping")
    return raw


def load_config(config_dir: Path | str) -> LoadedConfig:
    config_dir = Path(config_dir)

    jarvis_raw = expand_env(_load_yaml_file(config_dir / "jarvis.yaml"))
    # channels.yaml and mcp-servers.yaml are optional — loader tolerates
    # missing files and produces empty defaults so partial deployments work.
    channels_path = config_dir / "channels.yaml"
    mcp_path = config_dir / "mcp-servers.yaml"
    channels_raw = expand_env(_load_yaml_file(channels_path)) if channels_path.exists() else {}
    mcp_raw = expand_env(_load_yaml_file(mcp_path)) if mcp_path.exists() else {}

    try:
        jarvis_cfg = JarvisConfig.model_validate(jarvis_raw)
        channels_cfg = ChannelsConfig.model_validate(channels_raw)
        mcp_cfg = MCPServersConfig.model_validate(mcp_raw)
    except Exception as e:  # pydantic ValidationError or similar
        raise ConfigLoadError(str(e)) from e

    return LoadedConfig(jarvis=jarvis_cfg, channels=channels_cfg, mcp_servers=mcp_cfg)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/unit/test_config_loader.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/config/loader.py tests/unit/test_config_loader.py
git commit -m "add YAML config loader with env-var expansion"
```

---

## Task 13: Config watcher (hot-reload)

Spec references: §10 "YAML files are watched; on change, the loader validates and applies safe updates."

**Files:**
- Create: `jarvis/config/watcher.py`
- Create: `tests/integration/test_config_watcher.py`

- [ ] **Step 1: Write failing tests**

Write `tests/integration/test_config_watcher.py`:

```python
import asyncio
from pathlib import Path

import pytest

from jarvis.config.watcher import ConfigWatcher


def _write(p: Path, s: str) -> None:
    p.write_text(s)


@pytest.fixture
def config_dir(tmp_path):
    _write(
        tmp_path / "jarvis.yaml",
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: m
""",
    )
    _write(tmp_path / "channels.yaml", "{}")
    _write(tmp_path / "mcp-servers.yaml", "servers: []")
    return tmp_path


async def test_initial_load_fires_once(config_dir):
    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    watcher = ConfigWatcher(config_dir, on_change=on_change)
    await watcher.start()
    await asyncio.sleep(0.05)  # allow initial load to fire
    await watcher.stop()

    assert len(calls) == 1


async def test_edit_fires_reload(config_dir):
    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    watcher = ConfigWatcher(config_dir, on_change=on_change, debounce_sec=0.05)
    await watcher.start()
    await asyncio.sleep(0.1)

    # Edit a file.
    _write(
        config_dir / "jarvis.yaml",
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: CHANGED
""",
    )
    # Give watcher + debounce time to observe.
    await asyncio.sleep(0.5)
    await watcher.stop()

    assert len(calls) >= 2
    assert calls[-1].jarvis.llm.model == "CHANGED"


async def test_bad_edit_reports_error_and_keeps_old(config_dir):
    errors: list = []
    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    async def on_error(exc):
        errors.append(exc)

    watcher = ConfigWatcher(
        config_dir,
        on_change=on_change,
        on_error=on_error,
        debounce_sec=0.05,
    )
    await watcher.start()
    await asyncio.sleep(0.1)

    # Write invalid YAML.
    _write(config_dir / "jarvis.yaml", "llm: [not valid")
    await asyncio.sleep(0.5)
    await watcher.stop()

    assert len(errors) >= 1
    # Last successful config still callable (the one from the initial load).
    assert calls[-1].jarvis.llm.model == "m"
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/integration/test_config_watcher.py -v`
Expected: `ModuleNotFoundError` on `jarvis.config.watcher`.

- [ ] **Step 3: Write `jarvis/config/watcher.py`**

```python
"""Async config watcher: reloads on file changes, debounces, reports errors."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchfiles import awatch

from jarvis.config.loader import ConfigLoadError, LoadedConfig, load_config

_log = logging.getLogger(__name__)

OnChange = Callable[[LoadedConfig], Awaitable[None]]
OnError = Callable[[Exception], Awaitable[None]]


async def _noop_error(exc: Exception) -> None:  # noqa: ARG001
    return None


class ConfigWatcher:
    """Watches jarvis.yaml / channels.yaml / mcp-servers.yaml and reloads."""

    def __init__(
        self,
        config_dir: Path | str,
        *,
        on_change: OnChange,
        on_error: OnError | None = None,
        debounce_sec: float = 0.2,
    ) -> None:
        self._dir = Path(config_dir)
        self._on_change = on_change
        self._on_error = on_error or _noop_error
        self._debounce = debounce_sec
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        # Do an immediate load so callers have a baseline config.
        await self._try_load_and_emit()
        self._task = asyncio.create_task(self._run(), name="config-watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _try_load_and_emit(self) -> None:
        try:
            cfg = load_config(self._dir)
        except ConfigLoadError as e:
            _log.warning("config reload failed: %s", e)
            await self._on_error(e)
            return
        await self._on_change(cfg)

    async def _run(self) -> None:
        try:
            async for _ in awatch(self._dir, debounce=int(self._debounce * 1000)):
                if self._stopping.is_set():
                    return
                await self._try_load_and_emit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.exception("watcher loop error")
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/integration/test_config_watcher.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/config/watcher.py tests/integration/test_config_watcher.py
git commit -m "add async ConfigWatcher with debounced hot-reload"
```

---

## Task 14: Package entry point skeleton + full suite green

Spec references: §4.3 `main.py` wires it all together.

**Files:**
- Create: `jarvis/main.py`
- Create: `tests/integration/test_main_smoke.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_main_smoke.py`:

```python
from pathlib import Path

import pytest

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
        # Engine is live and DB file exists.
        assert db_path.exists()
    finally:
        await ctx.shutdown()
```

- [ ] **Step 2: Run test — verify it fails**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: `ModuleNotFoundError` on `jarvis.main`.

- [ ] **Step 3: Write `jarvis/main.py`**

```python
"""Application bootstrap — wires persistence, audit, config.

Later plans extend this to start channels, MCP manager, scheduler, and
the web dashboard. For now, bootstrap() returns an AppContext with the
infrastructure pieces initialized and a .shutdown() coroutine for teardown.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jarvis.audit.logger import AuditLogger
from jarvis.config.loader import LoadedConfig, load_config
from jarvis.persistence.db import Base, create_engine, session_factory

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    config: LoadedConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    audit: AuditLogger

    async def shutdown(self) -> None:
        await self.audit.stop()
        await self.engine.dispose()


async def bootstrap(*, config_dir: Path | str, db_url: str) -> AppContext:
    cfg = load_config(config_dir)
    logging.basicConfig(level=cfg.jarvis.log_level)

    engine = create_engine(db_url)
    # In Plan 1 we use metadata.create_all for simplicity in tests. Plan 6
    # will wire this through Alembic for the real deployment.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory)
    await audit.start()

    _log.info("jarvis bootstrap complete")
    return AppContext(
        config=cfg,
        engine=engine,
        session_factory=factory,
        audit=audit,
    )
```

- [ ] **Step 4: Run test — verify it passes**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest`
Expected: all tests pass (28+ tests across the plan).

- [ ] **Step 6: Run ruff**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: both pass.

- [ ] **Step 7: Commit**

```bash
git add jarvis/main.py tests/integration/test_main_smoke.py
git commit -m "add bootstrap entry point wiring config, DB, and AuditLogger"
```

---

## Plan 1 complete — summary

At this point the codebase has:

- Fully scaffolded Python project (`uv`, `ruff`, `pytest`).
- Core Pydantic types: `AuditEvent`, `InvocationRequest`, trigger variants.
- Async SQLAlchemy engine + session factory + `Base`.
- ORM models for all 8 tables from §7 of the spec.
- Alembic with an initial autogenerated migration (upgrade + downgrade tested).
- Repositories: `ConversationRepo` (with idle-timeout semantics), `MessageRepo`, `TriggerRepo`, `AuditRepo`, `ScheduleRepo`, `MCPServerRepo`, `MCPToolRepo`, `SettingsRepo`.
- `AuditLogger` — buffered async sink with batch + interval flushing.
- Config system: Pydantic schemas for all three YAML files, YAML loader with env-var expansion, watchfiles-based hot-reload watcher.
- `bootstrap()` entry point wiring config + DB + audit.

**Still to come (plans 2-6):** agent runner + MCP manager + tool policy + trigger dispatcher (Plan 2), Discord adapter + output router (Plan 3), scheduler (Plan 4), web dashboard (Plan 5), Docker packaging + E2E (Plan 6).

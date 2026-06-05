# Long-Term Memory And Preferences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add approval-gated preferences and automatic cross-channel vector recall over summarized Jarvis memories, with raw transcript fallback for exact recall.

**Architecture:** Add a focused `jarvis.memory` package that owns embedding, vector search, prompt assembly, summarization, and preference proposal behavior. Persist preferences, memory summaries, evidence snippets, vectors, and recall events in SQLite; use `sqlite-vec` for the summary vector table and keep raw messages in the existing transcript tables as exact-recall fallback. Wire a `MemoryService` into `AgentRunner` so preferences and recalled context are injected before `Runner.run`, and post-run summarization happens asynchronously after the response is persisted.

**Tech Stack:** Python 3.12, SQLAlchemy asyncio, Alembic, SQLite, `sqlite-vec`, OpenAI Python `AsyncOpenAI.embeddings.create`, OpenAI Agents SDK, FastAPI, Jinja2, HTMX, pytest, ruff.

---

## File Structure

- Modify `pyproject.toml` and `uv.lock` — add `sqlite-vec` dependency.
- Modify `config/jarvis.yaml.example` — add memory config defaults.
- Modify `jarvis/config/schema.py` — add `MemoryConfig` and attach it to `JarvisConfig`.
- Modify `jarvis/core/types.py` — add memory audit event types.
- Modify `jarvis/persistence/models.py` — add memory ORM rows.
- Modify `jarvis/persistence/repositories.py` — add memory repositories.
- Create `alembic/versions/0008_memory_preferences.py` — add memory tables and `sqlite-vec` vector table.
- Create `jarvis/memory/__init__.py` — package marker.
- Create `jarvis/memory/types.py` — dataclasses and typed records for prompt assembly and summarizer output.
- Create `jarvis/memory/embeddings.py` — embedding provider protocol plus OpenAI-compatible provider.
- Create `jarvis/memory/vector_store.py` — `sqlite-vec` load/probe/search/upsert wrapper.
- Create `jarvis/memory/prompt.py` — deterministic prompt assembly.
- Create `jarvis/memory/summarizer.py` — structured post-run summary generation.
- Create `jarvis/memory/service.py` — orchestration for preference loading, recall, recall-event persistence, summarization, and proposal creation.
- Modify `jarvis/agents/runner.py` — call memory service before and after runs.
- Modify `jarvis/main.py` — bootstrap memory service and pass it to `AgentRunner`.
- Create `jarvis/web/routes/memory.py` — `/memory` dashboard routes and form handlers.
- Create `jarvis/web/templates/memory.html` — memory dashboard page.
- Modify `jarvis/web/routes/conversations.py` and `jarvis/web/templates/conversation_detail.html` — show recalled memories on conversation detail.
- Modify `jarvis/web/templates/base.html` — add Memory nav link.
- Modify `README.md` — document memory behavior and config.
- Create focused tests under `tests/unit/` and `tests/integration/` as listed in tasks.

## Task 1: Config And Dependency Foundation

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `config/jarvis.yaml.example`
- Modify: `jarvis/config/schema.py`
- Test: `tests/unit/test_config_schema.py`

- [ ] **Step 1: Write failing config tests**

Append to `tests/unit/test_config_schema.py`:

```python
from jarvis.config.schema import JarvisConfig, LLMConfig


def test_memory_config_defaults_are_enabled_and_deterministic():
    cfg = JarvisConfig(llm=LLMConfig(base_url="http://x/v1", api_key="k", model="m"))

    assert cfg.memory.enabled is True
    assert cfg.memory.recall_enabled is True
    assert cfg.memory.embedding_model is None
    assert cfg.memory.embedding_dimensions == 1536
    assert cfg.memory.max_recalled_memories == 5
    assert cfg.memory.min_relevance_score == 0.25


def test_memory_config_accepts_embedding_override():
    cfg = JarvisConfig(
        llm=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        memory={
            "embedding_model": "text-embedding-3-small",
            "embedding_dimensions": 768,
            "max_recalled_memories": 3,
            "min_relevance_score": 0.4,
        },
    )

    assert cfg.memory.embedding_model == "text-embedding-3-small"
    assert cfg.memory.embedding_dimensions == 768
    assert cfg.memory.max_recalled_memories == 3
    assert cfg.memory.min_relevance_score == 0.4
```

- [ ] **Step 2: Run config tests and verify they fail**

Run:

```bash
uv run pytest tests/unit/test_config_schema.py::test_memory_config_defaults_are_enabled_and_deterministic tests/unit/test_config_schema.py::test_memory_config_accepts_embedding_override -q
```

Expected: FAIL with an error like `AttributeError: 'JarvisConfig' object has no attribute 'memory'`.

- [ ] **Step 3: Add `sqlite-vec` dependency**

Update `pyproject.toml` dependencies:

```toml
  "sqlite-vec>=0.1.6",
```

Place it near `aiosqlite` because it is part of persistence.

Run:

```bash
uv lock
```

Expected: `uv.lock` updates and `uv run python -c "import sqlite_vec; print(sqlite_vec.__name__)"` prints `sqlite_vec`.

- [ ] **Step 4: Add memory config schema**

In `jarvis/config/schema.py`, add below `LLMConfig`:

```python
class MemoryConfig(_StrictModel):
    enabled: bool = True
    recall_enabled: bool = True
    embedding_model: str | None = None
    embedding_dimensions: int = Field(default=1536, ge=1)
    max_recalled_memories: int = Field(default=5, ge=0, le=20)
    min_relevance_score: float = Field(default=0.25, ge=0.0)
```

Then add to `JarvisConfig`:

```python
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
```

- [ ] **Step 5: Add example config**

Append to `config/jarvis.yaml.example`:

```yaml
memory:
  enabled: true
  recall_enabled: true
  # Defaults to llm.model when omitted. Use an embedding-capable model if your
  # OpenAI-compatible endpoint exposes embeddings separately.
  embedding_model:
  embedding_dimensions: 1536
  max_recalled_memories: 5
  min_relevance_score: 0.25
```

- [ ] **Step 6: Run config tests and dependency import**

Run:

```bash
uv run pytest tests/unit/test_config_schema.py::test_memory_config_defaults_are_enabled_and_deterministic tests/unit/test_config_schema.py::test_memory_config_accepts_embedding_override -q
uv run python -c "import sqlite_vec; print('sqlite_vec ok')"
```

Expected: both tests PASS and the import command prints `sqlite_vec ok`.

- [ ] **Step 7: Commit**

Run:

```bash
git add pyproject.toml uv.lock config/jarvis.yaml.example jarvis/config/schema.py tests/unit/test_config_schema.py
git commit -m "feat: add memory configuration"
```

## Task 2: Persistence Schema, Migration, And Repositories

**Files:**
- Modify: `jarvis/core/types.py`
- Modify: `jarvis/persistence/models.py`
- Modify: `jarvis/persistence/repositories.py`
- Create: `alembic/versions/0008_memory_preferences.py`
- Test: `tests/integration/test_memory_migration.py`
- Test: `tests/integration/test_repositories_memory.py`

- [ ] **Step 1: Write failing migration test**

Create `tests/integration/test_memory_migration.py`:

```python
import os
import sqlite3
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


def test_memory_migration_roundtrip(tmp_path):
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            ).fetchall()
        }
        preference_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('memory_preferences')").fetchall()
        }
        entry_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('memory_entries')").fetchall()
        }
        evidence_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('memory_evidence')").fetchall()
        }
        recall_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('memory_recall_events')").fetchall()
        }
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list('memory_entries')").fetchall()
        }

    assert "memory_preferences" in tables
    assert "memory_entries" in tables
    assert "memory_evidence" in tables
    assert "memory_recall_events" in tables
    assert {"id", "content", "status", "source", "created_at", "updated_at"}.issubset(
        preference_columns
    )
    assert {"id", "conversation_id", "summary", "topics", "entities", "status"}.issubset(
        entry_columns
    )
    assert {"id", "memory_entry_id", "kind", "label", "content"}.issubset(evidence_columns)
    assert {"id", "conversation_id", "trigger_id", "memory_entry_id", "score", "rank"}.issubset(
        recall_columns
    )
    assert "ix_memory_entries_status_updated_at" in indexes

    down = _run_alembic(db_path, "downgrade 0007")
    assert down.returncode == 0, down.stderr

    with sqlite3.connect(db_path) as conn:
        tables_after_down = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            ).fetchall()
        }
    assert "memory_preferences" not in tables_after_down
    assert "memory_entries" not in tables_after_down
```

- [ ] **Step 2: Write failing repository tests**

Create `tests/integration/test_repositories_memory.py`:

```python
from uuid import uuid4

import pytest

from jarvis.core.types import ChannelKind
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    ConversationRepo,
    MemoryEntryRepo,
    MemoryPreferenceRepo,
    MemoryRecallRepo,
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


async def test_preference_lifecycle(session):
    repo = MemoryPreferenceRepo(session)

    pending = await repo.create_pending(content="Prefer concise answers.", source="dashboard")
    assert pending.status == "pending"

    await repo.approve(pending.id)
    active = await repo.list_active()
    assert [p.content for p in active] == ["Prefer concise answers."]

    await repo.archive(pending.id)
    assert await repo.list_active() == []


async def test_memory_entry_with_evidence_and_archive(session):
    conv = await ConversationRepo(session).find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    repo = MemoryEntryRepo(session)

    entry = await repo.create(
        conversation_id=conv.id,
        source_channel_kind=ChannelKind.DISCORD.value,
        source_channel_ref="user-1",
        summary="We discussed the Action Inbox PR.",
        topics=["jarvis", "actions"],
        entities=["PR #18"],
        evidence=[
            {
                "kind": "identifier",
                "label": "PR",
                "content": "PR #18",
            }
        ],
    )

    rows = await repo.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].summary == "We discussed the Action Inbox PR."
    evidence = await repo.list_evidence(entry.id)
    assert [(e.kind, e.label, e.content) for e in evidence] == [
        ("identifier", "PR", "PR #18")
    ]

    await repo.archive(entry.id)
    assert await repo.list_active_by_ids([entry.id]) == []


async def test_recall_events_are_ranked(session):
    conv = await ConversationRepo(session).find_or_create_open(
        channel_kind=ChannelKind.DASHBOARD,
        channel_ref="mark",
        idle_timeout_sec=900,
    )
    entry = await MemoryEntryRepo(session).create(
        conversation_id=conv.id,
        source_channel_kind=ChannelKind.DASHBOARD.value,
        source_channel_ref="mark",
        summary="Remembered project context.",
        topics=["jarvis"],
        entities=[],
        evidence=[],
    )

    recall_repo = MemoryRecallRepo(session)
    await recall_repo.record_many(
        conversation_id=conv.id,
        trigger_id=uuid4(),
        recalled=[{"memory_entry_id": entry.id, "score": 0.91, "rank": 1}],
    )

    events = await recall_repo.list_for_conversation(conv.id)
    assert len(events) == 1
    assert events[0].memory_entry_id == entry.id
    assert events[0].score == 0.91
    assert events[0].rank == 1
```

- [ ] **Step 3: Run persistence tests and verify they fail**

Run:

```bash
uv run pytest tests/integration/test_memory_migration.py tests/integration/test_repositories_memory.py -q
```

Expected: FAIL because memory ORM rows, repositories, and migration do not exist.

- [ ] **Step 4: Add audit event types**

In `jarvis/core/types.py`, add to `AuditEventType`:

```python
    MEMORY_PREFERENCE_PROPOSED = "memory.preference_proposed"
    MEMORY_PREFERENCE_APPROVED = "memory.preference_approved"
    MEMORY_PREFERENCE_REJECTED = "memory.preference_rejected"
    MEMORY_ENTRY_CREATED = "memory.entry_created"
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_FAILED = "memory.failed"
```

- [ ] **Step 5: Add ORM rows**

In `jarvis/persistence/models.py`, import `Float`:

```python
from sqlalchemy import JSON, Float, ForeignKey, Index, LargeBinary, String, Text
```

Add these ORM classes after `MessageRow`:

```python
class MemoryPreferenceRow(Base):
    __tablename__ = "memory_preferences"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    __table_args__ = (Index("ix_memory_preferences_status_updated_at", "status", "updated_at"),)


class MemoryEntryRow(Base):
    __tablename__ = "memory_entries"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_channel_kind: Mapped[str] = mapped_column(String(32))
    source_channel_ref: Mapped[str] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(Text)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())
    last_recalled_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    evidence: Mapped[list["MemoryEvidenceRow"]] = relationship(
        back_populates="memory_entry", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_memory_entries_status_updated_at", "status", "updated_at"),)


class MemoryEvidenceRow(Base):
    __tablename__ = "memory_evidence"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    memory_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_entries.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)

    memory_entry: Mapped[MemoryEntryRow] = relationship(back_populates="evidence")


class MemoryRecallEventRow(Base):
    __tablename__ = "memory_recall_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    memory_entry_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("memory_entries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    score: Mapped[float] = mapped_column(Float)
    rank: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
```

- [ ] **Step 6: Add migration**

Create `alembic/versions/0008_memory_preferences.py`:

```python
"""Memory preferences and recall tables.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from jarvis.persistence.db import TZDateTime

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", TZDateTime(), nullable=False),
        sa.Column("updated_at", TZDateTime(), nullable=False),
        sa.Column("approved_at", TZDateTime(), nullable=True),
        sa.Column("archived_at", TZDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_preferences_status_updated_at",
        "memory_preferences",
        ["status", "updated_at"],
    )
    op.create_index("ix_memory_preferences_status", "memory_preferences", ["status"])
    op.create_index("ix_memory_preferences_created_at", "memory_preferences", ["created_at"])

    op.create_table(
        "memory_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("source_channel_kind", sa.String(length=32), nullable=False),
        sa.Column("source_channel_ref", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", TZDateTime(), nullable=False),
        sa.Column("updated_at", TZDateTime(), nullable=False),
        sa.Column("last_recalled_at", TZDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_entries_conversation_id", "memory_entries", ["conversation_id"])
    op.create_index("ix_memory_entries_status", "memory_entries", ["status"])
    op.create_index("ix_memory_entries_created_at", "memory_entries", ["created_at"])
    op.create_index(
        "ix_memory_entries_status_updated_at",
        "memory_entries",
        ["status", "updated_at"],
    )

    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_entry_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", TZDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["memory_entry_id"], ["memory_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_evidence_memory_entry_id", "memory_evidence", ["memory_entry_id"])
    op.create_index("ix_memory_evidence_created_at", "memory_evidence", ["created_at"])

    op.create_table(
        "memory_recall_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        sa.Column("memory_entry_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", TZDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["memory_entry_id"], ["memory_entries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_recall_events_conversation_id",
        "memory_recall_events",
        ["conversation_id"],
    )
    op.create_index(
        "ix_memory_recall_events_trigger_id",
        "memory_recall_events",
        ["trigger_id"],
    )
    op.create_index(
        "ix_memory_recall_events_memory_entry_id",
        "memory_recall_events",
        ["memory_entry_id"],
    )
    op.create_index(
        "ix_memory_recall_events_created_at",
        "memory_recall_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_recall_events_created_at", table_name="memory_recall_events")
    op.drop_index("ix_memory_recall_events_memory_entry_id", table_name="memory_recall_events")
    op.drop_index("ix_memory_recall_events_trigger_id", table_name="memory_recall_events")
    op.drop_index("ix_memory_recall_events_conversation_id", table_name="memory_recall_events")
    op.drop_table("memory_recall_events")

    op.drop_index("ix_memory_evidence_created_at", table_name="memory_evidence")
    op.drop_index("ix_memory_evidence_memory_entry_id", table_name="memory_evidence")
    op.drop_table("memory_evidence")

    op.drop_index("ix_memory_entries_status_updated_at", table_name="memory_entries")
    op.drop_index("ix_memory_entries_created_at", table_name="memory_entries")
    op.drop_index("ix_memory_entries_status", table_name="memory_entries")
    op.drop_index("ix_memory_entries_conversation_id", table_name="memory_entries")
    op.drop_table("memory_entries")

    op.drop_index("ix_memory_preferences_created_at", table_name="memory_preferences")
    op.drop_index("ix_memory_preferences_status", table_name="memory_preferences")
    op.drop_index("ix_memory_preferences_status_updated_at", table_name="memory_preferences")
    op.drop_table("memory_preferences")
```

The `sqlite-vec` virtual table is created at runtime by `MemoryVectorStore` in Task 3 because Alembic should not fail schema migration on hosts where the extension cannot load. This preserves the spec requirement that Jarvis starts with preferences enabled and vector recall disabled when `sqlite-vec` is unavailable.

- [ ] **Step 7: Add repositories**

In `jarvis/persistence/repositories.py`, import the new rows:

```python
    MemoryEntryRow,
    MemoryEvidenceRow,
    MemoryPreferenceRow,
    MemoryRecallEventRow,
```

Add repository classes before `MCPServerRepo`:

```python
class MemoryPreferenceRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_pending(self, *, content: str, source: str) -> MemoryPreferenceRow:
        now = _utcnow()
        row = MemoryPreferenceRow(
            content=content,
            status="pending",
            source=source,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def list_active(self) -> list[MemoryPreferenceRow]:
        result = await self._session.execute(
            select(MemoryPreferenceRow)
            .where(MemoryPreferenceRow.status == "active")
            .order_by(MemoryPreferenceRow.updated_at.asc())
        )
        return list(result.scalars())

    async def list_for_dashboard(self, *, limit: int = 100) -> list[MemoryPreferenceRow]:
        result = await self._session.execute(
            select(MemoryPreferenceRow)
            .order_by(
                case((MemoryPreferenceRow.status == "pending", 0), else_=1),
                MemoryPreferenceRow.updated_at.desc(),
            )
            .limit(limit)
        )
        return list(result.scalars())

    async def approve(self, preference_id: UUID) -> None:
        now = _utcnow()
        await self._session.execute(
            update(MemoryPreferenceRow)
            .where(MemoryPreferenceRow.id == preference_id)
            .values(status="active", approved_at=now, updated_at=now, archived_at=None)
        )
        await self._session.commit()

    async def reject(self, preference_id: UUID) -> None:
        now = _utcnow()
        await self._session.execute(
            update(MemoryPreferenceRow)
            .where(MemoryPreferenceRow.id == preference_id)
            .values(status="rejected", updated_at=now)
        )
        await self._session.commit()

    async def archive(self, preference_id: UUID) -> None:
        now = _utcnow()
        await self._session.execute(
            update(MemoryPreferenceRow)
            .where(MemoryPreferenceRow.id == preference_id)
            .values(status="archived", archived_at=now, updated_at=now)
        )
        await self._session.commit()


class MemoryEntryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        conversation_id: UUID | None,
        source_channel_kind: str,
        source_channel_ref: str,
        summary: str,
        topics: list[str],
        entities: list[str],
        evidence: list[dict],
    ) -> MemoryEntryRow:
        now = _utcnow()
        row = MemoryEntryRow(
            conversation_id=conversation_id,
            source_channel_kind=source_channel_kind,
            source_channel_ref=source_channel_ref,
            summary=summary,
            topics=topics,
            entities=entities,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        for item in evidence:
            self._session.add(
                MemoryEvidenceRow(
                    memory_entry_id=row.id,
                    kind=str(item["kind"]),
                    label=str(item["label"]),
                    content=str(item["content"]),
                    created_at=now,
                )
            )
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def list_recent(self, *, limit: int = 100) -> list[MemoryEntryRow]:
        result = await self._session.execute(
            select(MemoryEntryRow).order_by(MemoryEntryRow.updated_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def list_active_by_ids(self, ids: list[UUID]) -> list[MemoryEntryRow]:
        if not ids:
            return []
        result = await self._session.execute(
            select(MemoryEntryRow).where(
                MemoryEntryRow.id.in_(ids),
                MemoryEntryRow.status == "active",
            )
        )
        by_id = {row.id: row for row in result.scalars()}
        return [by_id[i] for i in ids if i in by_id]

    async def list_evidence(self, memory_entry_id: UUID) -> list[MemoryEvidenceRow]:
        result = await self._session.execute(
            select(MemoryEvidenceRow)
            .where(MemoryEvidenceRow.memory_entry_id == memory_entry_id)
            .order_by(MemoryEvidenceRow.created_at.asc())
        )
        return list(result.scalars())

    async def archive(self, memory_entry_id: UUID) -> None:
        now = _utcnow()
        await self._session.execute(
            update(MemoryEntryRow)
            .where(MemoryEntryRow.id == memory_entry_id)
            .values(status="archived", updated_at=now)
        )
        await self._session.commit()

    async def mark_recalled(self, ids: list[UUID]) -> None:
        if not ids:
            return
        await self._session.execute(
            update(MemoryEntryRow)
            .where(MemoryEntryRow.id.in_(ids))
            .values(last_recalled_at=_utcnow())
        )
        await self._session.commit()


class MemoryRecallRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_many(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        recalled: list[dict],
    ) -> None:
        now = _utcnow()
        self._session.add_all(
            [
                MemoryRecallEventRow(
                    conversation_id=conversation_id,
                    trigger_id=trigger_id,
                    memory_entry_id=item["memory_entry_id"],
                    score=float(item["score"]),
                    rank=int(item["rank"]),
                    created_at=now,
                )
                for item in recalled
            ]
        )
        await self._session.commit()

    async def list_for_conversation(self, conversation_id: UUID) -> list[MemoryRecallEventRow]:
        result = await self._session.execute(
            select(MemoryRecallEventRow)
            .where(MemoryRecallEventRow.conversation_id == conversation_id)
            .order_by(MemoryRecallEventRow.rank.asc())
        )
        return list(result.scalars())
```

- [ ] **Step 8: Run persistence tests**

Run:

```bash
uv run pytest tests/integration/test_memory_migration.py tests/integration/test_repositories_memory.py -q
```

Expected: PASS.

- [ ] **Step 9: Run migration check**

Run:

```bash
uv run alembic check
```

Expected: PASS with no pending autogenerate operations.

- [ ] **Step 10: Commit**

Run:

```bash
git add jarvis/core/types.py jarvis/persistence/models.py jarvis/persistence/repositories.py alembic/versions/0008_memory_preferences.py tests/integration/test_memory_migration.py tests/integration/test_repositories_memory.py
git commit -m "feat: add memory persistence"
```

## Task 3: Embedding Provider And SQLite Vector Store

**Files:**
- Create: `jarvis/memory/__init__.py`
- Create: `jarvis/memory/types.py`
- Create: `jarvis/memory/embeddings.py`
- Create: `jarvis/memory/vector_store.py`
- Test: `tests/unit/test_memory_embeddings.py`
- Test: `tests/integration/test_memory_vector_store.py`

- [ ] **Step 1: Write failing embedding tests**

Create `tests/unit/test_memory_embeddings.py`:

```python
from types import SimpleNamespace

from jarvis.memory.embeddings import OpenAIEmbeddingProvider


class _FakeEmbeddings:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])


class _FakeClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


async def test_openai_embedding_provider_uses_configured_model():
    client = _FakeClient()
    provider = OpenAIEmbeddingProvider(
        client=client,
        model="text-embedding-3-small",
        dimensions=3,
    )

    got = await provider.embed("hello memory")

    assert got == [0.1, 0.2, 0.3]
    assert client.embeddings.calls == [
        {
            "input": "hello memory",
            "model": "text-embedding-3-small",
            "dimensions": 3,
        }
    ]
```

- [ ] **Step 2: Write failing vector store tests**

Create `tests/integration/test_memory_vector_store.py`:

```python
from uuid import uuid4

import pytest

from jarvis.memory.vector_store import MemoryVectorStore


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "vec.db"
    vector_store = MemoryVectorStore(db_path=db_path, dimensions=3)
    await vector_store.initialize()
    return vector_store


async def test_vector_store_upsert_and_search(store):
    first = uuid4()
    second = uuid4()

    await store.upsert(first, [0.1, 0.2, 0.3])
    await store.upsert(second, [0.9, 0.8, 0.7])

    results = await store.search([0.1, 0.2, 0.3], limit=2)

    assert [r.memory_entry_id for r in results] == [first, second]
    assert results[0].score >= results[1].score


async def test_vector_store_unavailable_is_reported(monkeypatch, tmp_path):
    def broken_load(_conn):
        raise RuntimeError("extension missing")

    monkeypatch.setattr("jarvis.memory.vector_store.sqlite_vec.load", broken_load)
    vector_store = MemoryVectorStore(db_path=tmp_path / "broken.db", dimensions=3)

    await vector_store.initialize()

    assert vector_store.available is False
    assert "extension missing" in vector_store.last_error
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/unit/test_memory_embeddings.py tests/integration/test_memory_vector_store.py -q
```

Expected: FAIL because `jarvis.memory` modules do not exist.

- [ ] **Step 4: Create memory package and types**

Create `jarvis/memory/__init__.py`:

```python
"""Long-term memory and preference support."""
```

Create `jarvis/memory/types.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class VectorSearchResult:
    memory_entry_id: UUID
    distance: float
    score: float


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    memory_entry_id: UUID
    summary: str
    topics: list[str]
    entities: list[str]
    evidence: list[dict[str, str]]
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class MemoryContext:
    preferences: list[str]
    recalled: list[RecalledMemory]
    recall_available: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemorySummary:
    summary: str
    topics: list[str]
    entities: list[str]
    evidence: list[dict[str, str]]
    preference_candidates: list[str]
```

- [ ] **Step 5: Implement embedding provider**

Create `jarvis/memory/embeddings.py`:

```python
from __future__ import annotations

from typing import Protocol

from openai import AsyncOpenAI


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class OpenAIEmbeddingProvider:
    def __init__(self, *, client: AsyncOpenAI, model: str, dimensions: int) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            input=text,
            model=self._model,
            dimensions=self._dimensions,
        )
        return list(response.data[0].embedding)
```

The local installed `openai` client exposes `AsyncOpenAI.embeddings.create(input=..., model=..., dimensions=...)`.

- [ ] **Step 6: Implement vector store**

Create `jarvis/memory/vector_store.py`:

```python
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import UUID

import sqlite_vec

from jarvis.memory.types import VectorSearchResult


class MemoryVectorStore:
    def __init__(self, *, db_path: Path, dimensions: int) -> None:
        self._db_path = db_path
        self._dimensions = dimensions
        self.available = False
        self.last_error: str | None = None

    async def initialize(self) -> None:
        try:
            await asyncio.to_thread(self._initialize_sync)
        except Exception as exc:
            self.available = False
            self.last_error = str(exc)
        else:
            self.available = True
            self.last_error = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_vector_ids (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_entry_id TEXT NOT NULL UNIQUE
                )
                """
            )
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors
                USING vec0(embedding float[{self._dimensions}])
                """
            )

    async def upsert(self, memory_entry_id: UUID, embedding: list[float]) -> None:
        if not self.available:
            return
        await asyncio.to_thread(self._upsert_sync, memory_entry_id, embedding)

    def _upsert_sync(self, memory_entry_id: UUID, embedding: list[float]) -> None:
        with self._connect() as conn:
            rowid = _rowid_for_memory(conn, memory_entry_id)
            conn.execute("DELETE FROM memory_vectors WHERE rowid = ?", (rowid,))
            conn.execute(
                "INSERT INTO memory_vectors(rowid, embedding) VALUES (?, ?)",
                (rowid, json.dumps(embedding)),
            )

    async def search(self, embedding: list[float], *, limit: int) -> list[VectorSearchResult]:
        if not self.available or limit <= 0:
            return []
        return await asyncio.to_thread(self._search_sync, embedding, limit)

    def _search_sync(self, embedding: list[float], limit: int) -> list[VectorSearchResult]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT memory_vectors.rowid, memory_vector_ids.memory_entry_id, distance
                FROM memory_vectors
                JOIN memory_vector_ids ON memory_vector_ids.rowid = memory_vectors.rowid
                WHERE embedding MATCH ?
                AND k = ?
                ORDER BY distance
                """,
                (json.dumps(embedding), limit),
            ).fetchall()
        return [
            VectorSearchResult(
                memory_entry_id=UUID(memory_entry_id),
                distance=float(distance),
                score=1.0 / (1.0 + float(distance)),
            )
            for _rowid, memory_entry_id, distance in rows
        ]


def _rowid_for_memory(conn: sqlite3.Connection, memory_entry_id: UUID) -> int:
    existing = conn.execute(
        "SELECT rowid FROM memory_vector_ids WHERE memory_entry_id = ?",
        (str(memory_entry_id),),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    cursor = conn.execute(
        "INSERT INTO memory_vector_ids(memory_entry_id) VALUES (?)",
        (str(memory_entry_id),),
    )
    return int(cursor.lastrowid)
```

- [ ] **Step 7: Run vector tests**

Run:

```bash
uv run pytest tests/unit/test_memory_embeddings.py tests/integration/test_memory_vector_store.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add jarvis/memory tests/unit/test_memory_embeddings.py tests/integration/test_memory_vector_store.py
git commit -m "feat: add memory embeddings and vector store"
```

## Task 4: Prompt Assembly And Memory Service Recall

**Files:**
- Create: `jarvis/memory/prompt.py`
- Create: `jarvis/memory/service.py`
- Test: `tests/unit/test_memory_prompt.py`
- Test: `tests/integration/test_memory_service.py`

- [ ] **Step 1: Write failing prompt tests**

Create `tests/unit/test_memory_prompt.py`:

```python
from uuid import uuid4

from jarvis.memory.prompt import assemble_memory_prompt
from jarvis.memory.types import MemoryContext, RecalledMemory


def test_assemble_memory_prompt_orders_preferences_context_and_current_prompt():
    recalled = RecalledMemory(
        memory_entry_id=uuid4(),
        summary="We discussed PR #18 Action Inbox deploy validation.",
        topics=["jarvis"],
        entities=["PR #18"],
        evidence=[{"kind": "identifier", "label": "PR", "content": "PR #18"}],
        score=0.9,
        rank=1,
    )
    ctx = MemoryContext(
        preferences=["Prefer concise answers."],
        recalled=[recalled],
        recall_available=True,
    )

    prompt = assemble_memory_prompt(
        memory_context=ctx,
        trigger_context="Schedule context:\n- Local date: 2026-06-04\n\n",
        current_prompt="What did we ship?",
    )

    assert prompt.index("Standing preferences") < prompt.index("Relevant prior context")
    assert prompt.index("Relevant prior context") < prompt.index("Schedule context")
    assert prompt.endswith("What did we ship?")
    assert "Prefer concise answers." in prompt
    assert "Use this as possibly relevant prior context" in prompt
    assert "PR #18" in prompt


def test_assemble_memory_prompt_without_memory_returns_current_prompt_only():
    ctx = MemoryContext(preferences=[], recalled=[], recall_available=True)

    assert assemble_memory_prompt(
        memory_context=ctx,
        trigger_context="",
        current_prompt="hello",
    ) == "hello"
```

- [ ] **Step 2: Write failing service tests**

Create `tests/integration/test_memory_service.py`:

```python
from uuid import uuid4

import pytest

from jarvis.core.types import ChannelKind
from jarvis.memory.service import MemoryService
from jarvis.memory.types import VectorSearchResult
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    ConversationRepo,
    MemoryEntryRepo,
    MemoryPreferenceRepo,
    MemoryRecallRepo,
)


class _FakeEmbeddingProvider:
    async def embed(self, text: str):
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    available = True
    last_error = None

    def __init__(self, result_id):
        self.result_id = result_id

    async def search(self, embedding, *, limit):
        return [VectorSearchResult(memory_entry_id=self.result_id, distance=0.1, score=0.91)]

    async def upsert(self, memory_entry_id, embedding):
        return None


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield factory
    await engine.dispose()


async def test_memory_service_builds_context_and_records_recall(factory):
    async with factory() as session:
        conv = await ConversationRepo(session).find_or_create_open(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="user-1",
            idle_timeout_sec=900,
        )
        await MemoryPreferenceRepo(session).create_pending(
            content="Prefer concise answers.",
            source="dashboard",
        )
        pref = (await MemoryPreferenceRepo(session).list_for_dashboard())[0]
        await MemoryPreferenceRepo(session).approve(pref.id)
        entry = await MemoryEntryRepo(session).create(
            conversation_id=conv.id,
            source_channel_kind=ChannelKind.DISCORD.value,
            source_channel_ref="user-1",
            summary="We discussed Action Inbox PR #18.",
            topics=["jarvis"],
            entities=["PR #18"],
            evidence=[{"kind": "identifier", "label": "PR", "content": "PR #18"}],
        )

    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(entry.id),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )
    trigger_id = uuid4()

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger_id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert len(ctx.recalled) == 1
    assert ctx.recalled[0].summary == "We discussed Action Inbox PR #18."

    async with factory() as session:
        events = await MemoryRecallRepo(session).list_for_conversation(conv.id)
    assert len(events) == 1
    assert events[0].memory_entry_id == entry.id


async def test_memory_service_continues_when_embedding_fails(factory):
    class BrokenEmbeddingProvider:
        async def embed(self, text: str):
            raise RuntimeError("embedding failed")

    service = MemoryService(
        session_factory=factory,
        embedding_provider=BrokenEmbeddingProvider(),
        vector_store=_FakeVectorStore(uuid4()),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    ctx = await service.build_context(
        conversation_id=None,
        trigger_id=None,
        prompt="hello",
    )

    assert ctx.preferences == []
    assert ctx.recalled == []
    assert ctx.recall_available is False
    assert "embedding failed" in ctx.error
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/unit/test_memory_prompt.py tests/integration/test_memory_service.py -q
```

Expected: FAIL because prompt and service modules do not exist.

- [ ] **Step 4: Implement prompt assembly**

Create `jarvis/memory/prompt.py`:

```python
from __future__ import annotations

from jarvis.memory.types import MemoryContext, RecalledMemory


def assemble_memory_prompt(
    *,
    memory_context: MemoryContext,
    trigger_context: str,
    current_prompt: str,
) -> str:
    sections: list[str] = []

    if memory_context.preferences:
        sections.append(
            "Standing preferences:\n"
            + "\n".join(f"- {preference}" for preference in memory_context.preferences)
        )

    if memory_context.recalled:
        sections.append(_format_recalled(memory_context.recalled))

    if trigger_context:
        sections.append(trigger_context.strip())

    sections.append(current_prompt)
    return "\n\n".join(section for section in sections if section)


def _format_recalled(recalled: list[RecalledMemory]) -> str:
    lines = [
        "Relevant prior context:",
        "Use this as possibly relevant prior context, not as a standing instruction.",
    ]
    for memory in recalled:
        lines.append(f"- {memory.summary}")
        if memory.topics:
            lines.append(f"  Topics: {', '.join(memory.topics)}")
        if memory.entities:
            lines.append(f"  Entities: {', '.join(memory.entities)}")
        for evidence in memory.evidence:
            label = evidence.get("label") or evidence.get("kind") or "evidence"
            content = evidence.get("content", "")
            if content:
                lines.append(f"  Evidence ({label}): {content}")
    return "\n".join(lines)
```

- [ ] **Step 5: Implement memory service recall**

Create `jarvis/memory/service.py`:

```python
from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.memory.embeddings import EmbeddingProvider
from jarvis.memory.types import MemoryContext, RecalledMemory
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.repositories import (
    MemoryEntryRepo,
    MemoryPreferenceRepo,
    MemoryRecallRepo,
)


class MemoryService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        vector_store: MemoryVectorStore,
        max_recalled_memories: int,
        min_relevance_score: float,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._max_recalled_memories = max_recalled_memories
        self._min_relevance_score = min_relevance_score

    async def build_context(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        prompt: str,
    ) -> MemoryContext:
        try:
            preferences = await self._load_preferences()
        except Exception as exc:
            preferences = []
            preference_error = str(exc)
        else:
            preference_error = None
        if not self._vector_store.available or self._max_recalled_memories <= 0:
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=self._vector_store.available,
                error=preference_error or self._vector_store.last_error,
            )

        try:
            embedding = await self._embedding_provider.embed(prompt)
            raw_results = await self._vector_store.search(
                embedding,
                limit=self._max_recalled_memories,
            )
        except Exception as exc:
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=False,
                error=preference_error or str(exc),
            )

        filtered = [
            result for result in raw_results if result.score >= self._min_relevance_score
        ][: self._max_recalled_memories]
        ids = [result.memory_entry_id for result in filtered]

        async with self._session_factory() as session:
            entry_repo = MemoryEntryRepo(session)
            entries = await entry_repo.list_active_by_ids(ids)
            by_id = {entry.id: entry for entry in entries}
            recalled: list[RecalledMemory] = []
            recall_rows: list[dict] = []
            for rank, result in enumerate(filtered, start=1):
                entry = by_id.get(result.memory_entry_id)
                if entry is None:
                    continue
                evidence_rows = await entry_repo.list_evidence(entry.id)
                recalled.append(
                    RecalledMemory(
                        memory_entry_id=entry.id,
                        summary=entry.summary,
                        topics=list(entry.topics or []),
                        entities=list(entry.entities or []),
                        evidence=[
                            {"kind": e.kind, "label": e.label, "content": e.content}
                            for e in evidence_rows
                        ],
                        score=result.score,
                        rank=rank,
                    )
                )
                recall_rows.append(
                    {"memory_entry_id": entry.id, "score": result.score, "rank": rank}
                )
            await MemoryRecallRepo(session).record_many(
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                recalled=recall_rows,
            )
            await entry_repo.mark_recalled([row["memory_entry_id"] for row in recall_rows])

        return MemoryContext(
            preferences=preferences,
            recalled=recalled,
            recall_available=True,
            error=preference_error,
        )

    async def _load_preferences(self) -> list[str]:
        async with self._session_factory() as session:
            rows = await MemoryPreferenceRepo(session).list_active()
        return [row.content for row in rows]
```

- [ ] **Step 6: Run prompt and service tests**

Run:

```bash
uv run pytest tests/unit/test_memory_prompt.py tests/integration/test_memory_service.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add jarvis/memory/prompt.py jarvis/memory/service.py tests/unit/test_memory_prompt.py tests/integration/test_memory_service.py
git commit -m "feat: recall memory context"
```

## Task 5: Summarizer And Post-Run Memory Creation

**Files:**
- Create: `jarvis/memory/summarizer.py`
- Modify: `jarvis/memory/service.py`
- Test: `tests/unit/test_memory_summarizer.py`
- Test: `tests/integration/test_memory_service_summarize.py`

- [ ] **Step 1: Write failing summarizer parsing tests**

Create `tests/unit/test_memory_summarizer.py`:

```python
from types import SimpleNamespace

from jarvis.memory.summarizer import MemorySummarizer


class _FakeCompletions:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=self.text)),
            ]
        )


class _FakeChat:
    def __init__(self, text):
        self.completions = _FakeCompletions(text)


class _FakeClient:
    def __init__(self, text):
        self.chat = _FakeChat(text)


async def test_summarizer_parses_structured_json():
    client = _FakeClient(
        """
        {
          "summary": "We discussed Jarvis memory.",
          "topics": ["jarvis", "memory"],
          "entities": ["sqlite-vec"],
          "evidence": [{"kind": "identifier", "label": "library", "content": "sqlite-vec"}],
          "preference_candidates": ["Prefer concise answers."]
        }
        """
    )
    summarizer = MemorySummarizer(client=client, model="m")

    got = await summarizer.summarize(
        user_prompt="let's add memory",
        assistant_output="sounds good",
    )

    assert got.summary == "We discussed Jarvis memory."
    assert got.topics == ["jarvis", "memory"]
    assert got.entities == ["sqlite-vec"]
    assert got.evidence[0]["content"] == "sqlite-vec"
    assert got.preference_candidates == ["Prefer concise answers."]
```

- [ ] **Step 2: Write failing post-run service tests**

Create `tests/integration/test_memory_service_summarize.py`:

```python
from uuid import uuid4

import pytest

from jarvis.core.types import ChannelKind
from jarvis.memory.service import MemoryService
from jarvis.memory.types import MemorySummary
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo


class _FakeEmbeddingProvider:
    async def embed(self, text: str):
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    available = True
    last_error = None

    def __init__(self):
        self.upserts = []

    async def search(self, embedding, *, limit):
        return []

    async def upsert(self, memory_entry_id, embedding):
        self.upserts.append((memory_entry_id, embedding))


class _FakeSummarizer:
    async def summarize(self, *, user_prompt: str, assistant_output: str):
        return MemorySummary(
            summary="We discussed Jarvis memory.",
            topics=["jarvis", "memory"],
            entities=["sqlite-vec"],
            evidence=[{"kind": "identifier", "label": "library", "content": "sqlite-vec"}],
            preference_candidates=["Prefer concise answers."],
        )


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield factory
    await engine.dispose()


async def test_summarize_run_creates_memory_entry_vector_and_preference_proposal(factory):
    vector_store = _FakeVectorStore()
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=vector_store,
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.summarize_run(
        conversation_id=uuid4(),
        channel_kind=ChannelKind.DASHBOARD.value,
        channel_ref="mark",
        user_prompt="let's add memory",
        assistant_output="done",
    )

    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent(limit=10)
        prefs = await MemoryPreferenceRepo(session).list_for_dashboard()

    assert len(entries) == 1
    assert entries[0].summary == "We discussed Jarvis memory."
    assert len(vector_store.upserts) == 1
    assert [p.content for p in prefs] == ["Prefer concise answers."]
    assert prefs[0].status == "pending"
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/unit/test_memory_summarizer.py tests/integration/test_memory_service_summarize.py -q
```

Expected: FAIL because summarizer and `summarize_run` do not exist.

- [ ] **Step 4: Implement summarizer**

Create `jarvis/memory/summarizer.py`:

```python
from __future__ import annotations

import json

from openai import AsyncOpenAI

from jarvis.memory.types import MemorySummary


class MemorySummarizer:
    def __init__(self, *, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def summarize(self, *, user_prompt: str, assistant_output: str) -> MemorySummary:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this Jarvis interaction for long-term recall. "
                        "Return strict JSON with keys summary, topics, entities, evidence, "
                        "and preference_candidates. Evidence items need kind, label, content."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"User prompt:\n{user_prompt}\n\n"
                        f"Assistant output:\n{assistant_output}"
                    ),
                },
            ],
        )
        text = _extract_text(response)
        payload = json.loads(text)
        return MemorySummary(
            summary=str(payload.get("summary", "")).strip(),
            topics=[str(item) for item in payload.get("topics", [])],
            entities=[str(item) for item in payload.get("entities", [])],
            evidence=[
                {
                    "kind": str(item.get("kind", "note")),
                    "label": str(item.get("label", item.get("kind", "note"))),
                    "content": str(item.get("content", "")),
                }
                for item in payload.get("evidence", [])
                if str(item.get("content", "")).strip()
            ],
            preference_candidates=[
                str(item).strip()
                for item in payload.get("preference_candidates", [])
                if str(item).strip()
            ],
        )


def _extract_text(response) -> str:
    choices = getattr(response, "choices", []) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    return str(content or "")
```

- [ ] **Step 5: Extend MemoryService**

Modify `jarvis/memory/service.py`:

```python
from jarvis.memory.types import MemoryContext, RecalledMemory, MemorySummary
```

Add optional `summarizer` to `__init__`:

```python
        summarizer=None,
```

Store it:

```python
        self._summarizer = summarizer
```

Add method:

```python
    async def summarize_run(
        self,
        *,
        conversation_id: UUID | None,
        channel_kind: str,
        channel_ref: str,
        user_prompt: str,
        assistant_output: str,
    ) -> None:
        if self._summarizer is None:
            return
        summary: MemorySummary = await self._summarizer.summarize(
            user_prompt=user_prompt,
            assistant_output=assistant_output,
        )
        if not summary.summary:
            return

        async with self._session_factory() as session:
            entry = await MemoryEntryRepo(session).create(
                conversation_id=conversation_id,
                source_channel_kind=channel_kind,
                source_channel_ref=channel_ref,
                summary=summary.summary,
                topics=summary.topics,
                entities=summary.entities,
                evidence=summary.evidence,
            )
            pref_repo = MemoryPreferenceRepo(session)
            for candidate in summary.preference_candidates:
                await pref_repo.create_pending(content=candidate, source="agent_proposal")

        if self._vector_store.available:
            embedding_text = "\n".join(
                [
                    summary.summary,
                    "Topics: " + ", ".join(summary.topics),
                    "Entities: " + ", ".join(summary.entities),
                ]
            )
            embedding = await self._embedding_provider.embed(embedding_text)
            await self._vector_store.upsert(entry.id, embedding)
```

If commits inside `MemoryEntryRepo.create()` conflict with later preference creation in the same session, split entry creation and preference creation into separate `async with self._session_factory()` blocks.

- [ ] **Step 6: Run summarizer tests**

Run:

```bash
uv run pytest tests/unit/test_memory_summarizer.py tests/integration/test_memory_service_summarize.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add jarvis/memory/summarizer.py jarvis/memory/service.py tests/unit/test_memory_summarizer.py tests/integration/test_memory_service_summarize.py
git commit -m "feat: summarize memory entries"
```

## Task 6: Agent Runner And Bootstrap Integration

**Files:**
- Modify: `jarvis/agents/runner.py`
- Modify: `jarvis/main.py`
- Test: `tests/integration/test_agent_runner_memory.py`
- Test: `tests/integration/test_main_smoke.py`

- [ ] **Step 1: Write failing runner memory tests**

Create `tests/integration/test_agent_runner_memory.py`:

```python
from types import SimpleNamespace

import pytest_asyncio

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.types import InvocationRequest, ManualTrigger
from jarvis.memory.types import MemoryContext
from jarvis.persistence.db import Base, create_engine, session_factory


class _FakeMemoryService:
    def __init__(self):
        self.build_calls = []
        self.summarize_calls = []

    async def build_context(self, *, conversation_id, trigger_id, prompt):
        self.build_calls.append(
            {"conversation_id": conversation_id, "trigger_id": trigger_id, "prompt": prompt}
        )
        return MemoryContext(
            preferences=["Prefer concise answers."],
            recalled=[],
            recall_available=True,
        )

    async def summarize_run(self, **kwargs):
        self.summarize_calls.append(kwargs)


@pytest_asyncio.fixture(loop_scope="function")
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


async def test_agent_runner_injects_memory_context_and_summarizes(infra, monkeypatch):
    factory, audit = infra
    captured = {}

    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output="done")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)
    memory_service = _FakeMemoryService()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        memory_service=memory_service,
    )

    await runner.run(InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hello")))

    assert "Standing preferences:" in captured["prompt"]
    assert "Prefer concise answers." in captured["prompt"]
    assert captured["prompt"].endswith("hello")
    assert len(memory_service.build_calls) == 1
    assert len(memory_service.summarize_calls) == 1
    assert memory_service.summarize_calls[0]["assistant_output"] == "done"


async def test_agent_runner_continues_when_memory_recall_fails(infra, monkeypatch):
    factory, audit = infra
    captured = {}

    class BrokenMemoryService:
        async def build_context(self, **kwargs):
            raise RuntimeError("memory down")

        async def summarize_run(self, **kwargs):
            raise RuntimeError("summary down")

    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output="done")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        memory_service=BrokenMemoryService(),
    )

    result = await runner.run(InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hello")))

    assert result.final_output == "done"
    assert captured["prompt"] == "hello"
```

- [ ] **Step 2: Run runner memory tests and verify they fail**

Run:

```bash
uv run pytest tests/integration/test_agent_runner_memory.py -q
```

Expected: FAIL because `AgentRunner.__init__` does not accept `memory_service`.

- [ ] **Step 3: Modify AgentRunner constructor**

In `jarvis/agents/runner.py`, import:

```python
import asyncio
```

and:

```python
from jarvis.memory.prompt import assemble_memory_prompt
from jarvis.memory.types import MemoryContext
```

Add constructor parameter:

```python
        memory_service: Any = None,
```

Store it:

```python
        self._memory_service = memory_service
```

- [ ] **Step 4: Split scheduled trigger context from prompt**

Replace `_extract_from_trigger()` use in `run()` with:

```python
        channel_kind, channel_ref, user_prompt, trigger_context = _extract_from_trigger(request)
```

Update `_extract_from_trigger()`:

```python
def _extract_from_trigger(request: InvocationRequest):
    t = request.trigger
    if isinstance(t, ChannelMessage):
        return t.channel_kind, t.channel_ref, t.text, ""
    if isinstance(t, ScheduledTrigger):
        return ChannelKind.SCHEDULED, t.schedule_id, t.prompt, _scheduled_context(t)
    if isinstance(t, ManualTrigger):
        return ChannelKind.DASHBOARD, t.user, t.prompt, ""
    raise ValueError(f"unknown trigger: {t!r}")
```

Replace `_scheduled_prompt()` with:

```python
def _scheduled_context(trigger: ScheduledTrigger) -> str:
    if trigger.timezone is None or trigger.fired_at is None:
        return ""

    try:
        zone = ZoneInfo(trigger.timezone)
    except ZoneInfoNotFoundError:
        return ""

    fired_at_utc = trigger.fired_at
    if fired_at_utc.tzinfo is None:
        fired_at_utc = fired_at_utc.replace(tzinfo=UTC)
    local_time = fired_at_utc.astimezone(zone)
    return (
        "Schedule context:\n"
        f"- Timezone: {trigger.timezone}\n"
        f"- Local date: {local_time:%Y-%m-%d}\n"
        f"- Local time: {local_time:%Y-%m-%d %H:%M %Z}\n"
        "- Interpret relative dates like today, tomorrow, and yesterday in this timezone."
    )
```

Use `prompt = user_prompt` initially, then after `conv_id` and `trigger_id` exist:

```python
        prompt = await self._build_prompt_with_memory(
            conversation_id=conv_id,
            trigger_id=trigger_id,
            trigger_context=trigger_context,
            user_prompt=user_prompt,
        )
```

Add helper:

```python
    async def _build_prompt_with_memory(
        self,
        *,
        conversation_id: UUID,
        trigger_id: UUID,
        trigger_context: str,
        user_prompt: str,
    ) -> str:
        if self._memory_service is None:
            return f"{trigger_context.strip()}\n\n{user_prompt}" if trigger_context else user_prompt
        try:
            memory_context = await self._memory_service.build_context(
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                prompt=user_prompt,
            )
        except Exception:
            _log.exception("memory recall failed")
            memory_context = MemoryContext(preferences=[], recalled=[], recall_available=False)
        return assemble_memory_prompt(
            memory_context=memory_context,
            trigger_context=trigger_context,
            current_prompt=user_prompt,
        )
```

- [ ] **Step 5: Schedule post-run summarization**

After final assistant message persistence, before returning `AgentRunResult`, add:

```python
        self._schedule_memory_summary(
            conversation_id=conv_id,
            channel_kind=channel_kind.value,
            channel_ref=channel_ref,
            user_prompt=user_prompt,
            assistant_output=final_text,
        )
```

Add method:

```python
    def _schedule_memory_summary(
        self,
        *,
        conversation_id: UUID,
        channel_kind: str,
        channel_ref: str,
        user_prompt: str,
        assistant_output: str,
    ) -> None:
        if self._memory_service is None:
            return

        async def _run() -> None:
            try:
                await self._memory_service.summarize_run(
                    conversation_id=conversation_id,
                    channel_kind=channel_kind,
                    channel_ref=channel_ref,
                    user_prompt=user_prompt,
                    assistant_output=assistant_output,
                )
            except Exception:
                _log.exception("memory summarization failed")

        asyncio.create_task(_run(), name="memory-summary")
```

In tests, if the async task has not completed before assertion, change the test to `await asyncio.sleep(0)` after `runner.run()`.

- [ ] **Step 6: Run agent runner tests**

Run:

```bash
uv run pytest tests/integration/test_agent_runner.py tests/integration/test_agent_runner_memory.py -q
```

Expected: PASS, including the existing scheduled local-date context test.

- [ ] **Step 7: Wire bootstrap**

In `jarvis/main.py`, import:

```python
from jarvis.memory.embeddings import OpenAIEmbeddingProvider
from jarvis.memory.service import MemoryService
from jarvis.memory.summarizer import MemorySummarizer
from jarvis.memory.vector_store import MemoryVectorStore
```

Add `memory_service: MemoryService | None` to `AppContext`.

After `model_store.load()`, build memory objects:

```python
    memory_service = None
    if cfg.jarvis.memory.enabled:
        embedding_model = cfg.jarvis.memory.embedding_model or cfg.jarvis.llm.model
        vector_store = MemoryVectorStore(
            db_path=Path(db_url.removeprefix("sqlite+aiosqlite:///")),
            dimensions=cfg.jarvis.memory.embedding_dimensions,
        )
        await vector_store.initialize()
        embedding_provider = OpenAIEmbeddingProvider(
            client=llm_client,
            model=embedding_model,
            dimensions=cfg.jarvis.memory.embedding_dimensions,
        )
        memory_service = MemoryService(
            session_factory=factory,
            embedding_provider=embedding_provider,
            vector_store=vector_store,
            summarizer=MemorySummarizer(client=llm_client, model=cfg.jarvis.llm.model),
            max_recalled_memories=cfg.jarvis.memory.max_recalled_memories
            if cfg.jarvis.memory.recall_enabled
            else 0,
            min_relevance_score=cfg.jarvis.memory.min_relevance_score,
        )
```

Pass `memory_service=memory_service` to `AgentRunner`.

If `db_url` is not a local SQLite URL, set `memory_service = None` and log a warning; Jarvis currently uses SQLite in all supported deployment paths.

- [ ] **Step 8: Run main smoke tests**

Run:

```bash
uv run pytest tests/integration/test_main_smoke.py tests/integration/test_agent_runner_memory.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
git add jarvis/agents/runner.py jarvis/main.py tests/integration/test_agent_runner_memory.py tests/integration/test_main_smoke.py
git commit -m "feat: wire memory into agent runs"
```

## Task 7: Memory Dashboard And Conversation Recall Display

**Files:**
- Create: `jarvis/web/routes/memory.py`
- Create: `jarvis/web/templates/memory.html`
- Modify: `jarvis/web/app.py`
- Modify: `jarvis/web/templates/base.html`
- Modify: `jarvis/web/routes/conversations.py`
- Modify: `jarvis/web/templates/conversation_detail.html`
- Test: `tests/integration/test_web_memory.py`
- Test: `tests/integration/test_web_conversations.py`

- [ ] **Step 1: Write failing web memory tests**

Create `tests/integration/test_web_memory.py`:

```python
from unittest.mock import MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.core.types import ChannelKind
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    ConversationRepo,
    MemoryEntryRepo,
    MemoryPreferenceRepo,
)
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    async with factory() as session:
        pref = await MemoryPreferenceRepo(session).create_pending(
            content="Prefer concise answers.",
            source="dashboard",
        )
        await MemoryPreferenceRepo(session).approve(pref.id)
        conv = await ConversationRepo(session).find_or_create_open(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="user-1",
            idle_timeout_sec=900,
        )
        entry = await MemoryEntryRepo(session).create(
            conversation_id=conv.id,
            source_channel_kind=ChannelKind.DISCORD.value,
            source_channel_ref="user-1",
            summary="We discussed Action Inbox PR #18.",
            topics=["jarvis"],
            entities=["PR #18"],
            evidence=[{"kind": "identifier", "label": "PR", "content": "PR #18"}],
        )

    ctx = MagicMock()
    ctx.session_factory = factory
    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield client, factory, pref.id, entry.id
    await engine.dispose()


def test_memory_page_lists_preferences_and_entries(client_and_factory):
    client, _, _, _ = client_and_factory

    resp = client.get("/memory")

    assert resp.status_code == 200
    assert "Prefer concise answers." in resp.text
    assert "We discussed Action Inbox PR #18." in resp.text
    assert "PR #18" in resp.text


def test_memory_page_archives_entry(client_and_factory):
    client, _, _, entry_id = client_and_factory

    resp = client.post(f"/memory/entries/{entry_id}/archive", follow_redirects=False)

    assert resp.status_code == 303
```

- [ ] **Step 2: Extend conversation detail test**

Append to `tests/integration/test_web_conversations.py`:

```python
async def test_conversation_detail_shows_recalled_memories(ctx_and_client):
    _, client, conv_id, factory = ctx_and_client
    from jarvis.persistence.repositories import MemoryEntryRepo, MemoryRecallRepo

    async with factory() as session:
        entry = await MemoryEntryRepo(session).create(
            conversation_id=conv_id,
            source_channel_kind="discord",
            source_channel_ref="user-1",
            summary="We recalled Action Inbox context.",
            topics=["jarvis"],
            entities=[],
            evidence=[],
        )
        await MemoryRecallRepo(session).record_many(
            conversation_id=conv_id,
            trigger_id=None,
            recalled=[{"memory_entry_id": entry.id, "score": 0.88, "rank": 1}],
        )

    resp = client.get(f"/conversations/{conv_id}")

    assert resp.status_code == 200
    assert "Recalled memories" in resp.text
    assert "We recalled Action Inbox context." in resp.text
```

- [ ] **Step 3: Run web tests and verify they fail**

Run:

```bash
uv run pytest tests/integration/test_web_memory.py tests/integration/test_web_conversations.py::test_conversation_detail_shows_recalled_memories -q
```

Expected: FAIL because `/memory` is not registered and conversation detail does not load recall events.

- [ ] **Step 4: Implement memory routes**

Create `jarvis/web/routes/memory.py`:

```python
"""Memory dashboard routes."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo

router = APIRouter()


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard(limit=100)
        entries = await MemoryEntryRepo(session).list_recent(limit=100)
        evidence_by_entry = {
            entry.id: await MemoryEntryRepo(session).list_evidence(entry.id)
            for entry in entries
        }
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "preferences": preferences,
            "entries": entries,
            "evidence_by_entry": evidence_by_entry,
        },
    )


@router.post("/memory/preferences/{preference_id}/approve")
async def approve_preference(request: Request, preference_id: UUID):
    async with request.app.state.ctx.session_factory() as session:
        await MemoryPreferenceRepo(session).approve(preference_id)
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/memory/preferences/{preference_id}/reject")
async def reject_preference(request: Request, preference_id: UUID):
    async with request.app.state.ctx.session_factory() as session:
        await MemoryPreferenceRepo(session).reject(preference_id)
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/memory/preferences/{preference_id}/archive")
async def archive_preference(request: Request, preference_id: UUID):
    async with request.app.state.ctx.session_factory() as session:
        await MemoryPreferenceRepo(session).archive(preference_id)
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/memory/entries/{entry_id}/archive")
async def archive_entry(request: Request, entry_id: UUID):
    async with request.app.state.ctx.session_factory() as session:
        await MemoryEntryRepo(session).archive(entry_id)
    return RedirectResponse(url="/memory", status_code=303)
```

Remove `HTTPException` if ruff reports it unused.

- [ ] **Step 5: Implement template and nav**

Create `jarvis/web/templates/memory.html`:

```html
{% extends "base.html" %}
{% block title %}Memory{% endblock %}
{% block content %}
<section class="page-head">
    <div>
        <h1>Memory</h1>
        <p class="muted">Approved preferences and automatic recall summaries.</p>
    </div>
</section>

<section class="section-block">
<h2>Preferences</h2>
<table class="ops-table">
    <thead><tr><th>Status</th><th>Preference</th><th>Source</th><th>Updated</th><th></th></tr></thead>
    <tbody>
    {% for pref in preferences %}
        <tr>
            <td><span class="badge {% if pref.status == 'active' %}badge-ok{% elif pref.status == 'pending' %}badge-warn{% else %}badge-err{% endif %}">{{ pref.status }}</span></td>
            <td>{{ pref.content }}</td>
            <td class="muted">{{ pref.source }}</td>
            <td class="muted">{{ pref.updated_at.strftime('%Y-%m-%d %H:%M') }}</td>
            <td>
                {% if pref.status == 'pending' %}
                <form class="inline-form" method="post" action="/memory/preferences/{{ pref.id }}/approve"><button type="submit">Approve</button></form>
                <form class="inline-form" method="post" action="/memory/preferences/{{ pref.id }}/reject"><button class="btn-danger" type="submit">Reject</button></form>
                {% endif %}
                {% if pref.status != 'archived' %}
                <form class="inline-form" method="post" action="/memory/preferences/{{ pref.id }}/archive"><button type="submit">Archive</button></form>
                {% endif %}
            </td>
        </tr>
    {% endfor %}
    </tbody>
</table>
{% if not preferences %}<p class="muted">No preferences yet.</p>{% endif %}
</section>

<section class="section-block">
<h2>Recall Memories</h2>
<table class="ops-table">
    <thead><tr><th>Status</th><th>Summary</th><th>Topics</th><th>Entities</th><th>Evidence</th><th></th></tr></thead>
    <tbody>
    {% for entry in entries %}
        <tr>
            <td><span class="badge {% if entry.status == 'active' %}badge-ok{% else %}badge-err{% endif %}">{{ entry.status }}</span></td>
            <td>{{ entry.summary }}</td>
            <td class="muted">{{ entry.topics | join(", ") }}</td>
            <td class="muted">{{ entry.entities | join(", ") }}</td>
            <td>
                {% for ev in evidence_by_entry.get(entry.id, []) %}
                    <div class="muted"><strong>{{ ev.label }}</strong>: {{ ev.content }}</div>
                {% endfor %}
            </td>
            <td>
                {% if entry.status != 'archived' %}
                <form class="inline-form" method="post" action="/memory/entries/{{ entry.id }}/archive"><button type="submit">Archive</button></form>
                {% endif %}
            </td>
        </tr>
    {% endfor %}
    </tbody>
</table>
{% if not entries %}<p class="muted">No recall memories yet.</p>{% endif %}
</section>
{% endblock %}
```

Add to `jarvis/web/templates/base.html` nav after Actions:

```html
            <a href="/memory">Memory</a>
```

- [ ] **Step 6: Register route**

In `jarvis/web/app.py`, include after actions:

```python
    from jarvis.web.routes.memory import router as memory_router

    app.include_router(memory_router)
```

- [ ] **Step 7: Show recall events on conversation detail**

In `jarvis/web/routes/conversations.py`, import `MemoryEntryRepo` and `MemoryRecallRepo`. In `conversation_detail`, after `messages`:

```python
        recall_events = await MemoryRecallRepo(session).list_for_conversation(conv_id) if conv else []
        memory_entries = await MemoryEntryRepo(session).list_active_by_ids(
            [event.memory_entry_id for event in recall_events if event.memory_entry_id]
        )
        memories_by_id = {entry.id: entry for entry in memory_entries}
```

Pass:

```python
            "recall_events": recall_events,
            "memories_by_id": memories_by_id,
```

In `jarvis/web/templates/conversation_detail.html`, after messages:

```html
<section class="section-block">
<h2>Recalled memories</h2>
{% if recall_events %}
<table class="ops-table">
    <thead><tr><th>Rank</th><th>Score</th><th>Summary</th></tr></thead>
    <tbody>
    {% for event in recall_events %}
        {% set memory = memories_by_id.get(event.memory_entry_id) %}
        {% if memory %}
        <tr>
            <td>{{ event.rank }}</td>
            <td>{{ "%.3f"|format(event.score) }}</td>
            <td>{{ memory.summary }}</td>
        </tr>
        {% endif %}
    {% endfor %}
    </tbody>
</table>
{% else %}
<p class="muted">No memories were recalled for this conversation.</p>
{% endif %}
</section>
```

- [ ] **Step 8: Run web tests**

Run:

```bash
uv run pytest tests/integration/test_web_memory.py tests/integration/test_web_conversations.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
git add jarvis/web/app.py jarvis/web/routes/memory.py jarvis/web/routes/conversations.py jarvis/web/templates/base.html jarvis/web/templates/memory.html jarvis/web/templates/conversation_detail.html tests/integration/test_web_memory.py tests/integration/test_web_conversations.py
git commit -m "feat: add memory dashboard"
```

## Task 8: Memory Audit Events

**Files:**
- Modify: `jarvis/memory/service.py`
- Modify: `jarvis/main.py`
- Test: `tests/integration/test_memory_audit.py`

- [ ] **Step 1: Write failing audit tests**

Create `tests/integration/test_memory_audit.py`:

```python
from uuid import uuid4

import pytest

from jarvis.core.types import AuditEventType, ChannelKind
from jarvis.memory.service import MemoryService
from jarvis.memory.types import MemorySummary, VectorSearchResult
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ConversationRepo, MemoryEntryRepo


class _RecordingAudit:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class _FakeEmbeddingProvider:
    async def embed(self, text: str):
        return [0.1, 0.2, 0.3]


class _BrokenEmbeddingProvider:
    async def embed(self, text: str):
        raise RuntimeError("embedding failed")


class _FakeVectorStore:
    available = True
    last_error = None

    def __init__(self, result_id=None):
        self.result_id = result_id
        self.upserts = []

    async def search(self, embedding, *, limit):
        if self.result_id is None:
            return []
        return [VectorSearchResult(memory_entry_id=self.result_id, distance=0.1, score=0.91)]

    async def upsert(self, memory_entry_id, embedding):
        self.upserts.append((memory_entry_id, embedding))


class _FakeSummarizer:
    async def summarize(self, *, user_prompt: str, assistant_output: str):
        return MemorySummary(
            summary="We discussed Jarvis memory.",
            topics=["jarvis"],
            entities=[],
            evidence=[],
            preference_candidates=["Prefer concise answers."],
        )


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield factory
    await engine.dispose()


async def test_memory_recall_emits_audit_event(factory):
    async with factory() as session:
        conv = await ConversationRepo(session).find_or_create_open(
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="mark",
            idle_timeout_sec=900,
        )
        entry = await MemoryEntryRepo(session).create(
            conversation_id=conv.id,
            source_channel_kind=ChannelKind.DASHBOARD.value,
            source_channel_ref="mark",
            summary="We discussed Jarvis memory.",
            topics=["jarvis"],
            entities=[],
            evidence=[],
        )
    audit = _RecordingAudit()
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(entry.id),
        audit=audit,
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.build_context(conversation_id=conv.id, trigger_id=uuid4(), prompt="memory?")

    assert [event.type for event in audit.events] == [AuditEventType.MEMORY_RECALLED]
    assert audit.events[0].payload["count"] == 1


async def test_memory_failure_emits_audit_event(factory):
    audit = _RecordingAudit()
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_BrokenEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        audit=audit,
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.build_context(conversation_id=None, trigger_id=None, prompt="memory?")

    assert [event.type for event in audit.events] == [AuditEventType.MEMORY_FAILED]
    assert "embedding failed" in audit.events[0].payload["error"]


async def test_memory_summary_emits_creation_and_preference_events(factory):
    audit = _RecordingAudit()
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FakeSummarizer(),
        audit=audit,
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.summarize_run(
        conversation_id=uuid4(),
        channel_kind=ChannelKind.DASHBOARD.value,
        channel_ref="mark",
        user_prompt="hello",
        assistant_output="done",
    )

    assert [event.type for event in audit.events] == [
        AuditEventType.MEMORY_ENTRY_CREATED,
        AuditEventType.MEMORY_PREFERENCE_PROPOSED,
    ]
```

- [ ] **Step 2: Run audit tests and verify they fail**

Run:

```bash
uv run pytest tests/integration/test_memory_audit.py -q
```

Expected: FAIL because `MemoryService` does not accept `audit` and does not emit events.

- [ ] **Step 3: Add audit support to MemoryService**

Modify `jarvis/memory/service.py` imports:

```python
from jarvis.core.types import AuditEvent, AuditEventType
```

Add constructor parameter:

```python
        audit=None,
```

Store it:

```python
        self._audit = audit
```

Add helper:

```python
    async def _emit(self, event_type: AuditEventType, *, conversation_id=None, trigger_id=None, payload=None) -> None:
        if self._audit is None:
            return
        await self._audit.emit(
            AuditEvent(
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                type=event_type,
                payload=payload or {},
            )
        )
```

In `build_context()`, when preference loading fails, call:

```python
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                payload={"stage": "preferences", "error": preference_error},
            )
```

In the embedding/search exception block, call:

```python
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                payload={"stage": "recall", "error": str(exc)},
            )
```

After recall rows are recorded, call:

```python
        await self._emit(
            AuditEventType.MEMORY_RECALLED,
            conversation_id=conversation_id,
            trigger_id=trigger_id,
            payload={"count": len(recalled), "memory_entry_ids": [str(m.memory_entry_id) for m in recalled]},
        )
```

In `summarize_run()`, after creating the entry, call:

```python
        await self._emit(
            AuditEventType.MEMORY_ENTRY_CREATED,
            conversation_id=conversation_id,
            payload={"memory_entry_id": str(entry.id)},
        )
```

After each preference proposal, call:

```python
                await self._emit(
                    AuditEventType.MEMORY_PREFERENCE_PROPOSED,
                    conversation_id=conversation_id,
                    payload={"content": candidate},
                )
```

Wrap summarization in `try/except Exception as exc`, emit `MEMORY_FAILED` with `stage="summarize"`, and re-raise so `AgentRunner` still logs the background task failure.

- [ ] **Step 4: Pass audit from bootstrap**

In `jarvis/main.py`, update the `MemoryService` constructor call:

```python
            audit=audit,
```

- [ ] **Step 5: Run audit tests**

Run:

```bash
uv run pytest tests/integration/test_memory_audit.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add jarvis/memory/service.py jarvis/main.py tests/integration/test_memory_audit.py
git commit -m "feat: audit memory lifecycle"
```

## Task 9: Documentation, Full Verification, And Browser Smoke

**Files:**
- Modify: `README.md`
- Test: existing full test suite

- [ ] **Step 1: Update README dashboard list**

In `README.md`, add Memory to the Dashboard list:

```markdown
- **Memory** — approved preferences, recall summaries, evidence snippets, and recall debugging
```

- [ ] **Step 2: Add memory configuration docs**

In `README.md`, after Model selection, add:

```markdown
### Long-term memory and preferences

Jarvis has two separate memory lanes:

- **Preferences** are approved standing instructions that shape future behavior.
  Pending preference proposals appear on the Memory page and only active
  preferences are injected into runs.
- **Recall memories** are compact summaries of prior conversations. Jarvis
  embeds summaries with `sqlite-vec`, searches them automatically across
  Discord, dashboard, and scheduled runs, and injects relevant memories as
  prior context. Raw transcripts remain available for exact recall requests,
  but Jarvis does not embed every raw message in v1.

Memory config lives in `jarvis.yaml`:

```yaml
memory:
  enabled: true
  recall_enabled: true
  embedding_model:
  embedding_dimensions: 1536
  max_recalled_memories: 5
  min_relevance_score: 0.25
```

If `sqlite-vec` cannot load, Jarvis continues running with preferences enabled
and automatic vector recall disabled.
```

- [ ] **Step 3: Run formatting and focused tests**

Run:

```bash
uv run ruff check jarvis tests
uv run pytest tests/unit/test_config_schema.py tests/unit/test_memory_embeddings.py tests/unit/test_memory_prompt.py tests/unit/test_memory_summarizer.py -q
uv run pytest tests/integration/test_memory_migration.py tests/integration/test_repositories_memory.py tests/integration/test_memory_vector_store.py tests/integration/test_memory_service.py tests/integration/test_memory_service_summarize.py tests/integration/test_memory_audit.py tests/integration/test_agent_runner_memory.py tests/integration/test_web_memory.py -q
```

Expected: all PASS.

- [ ] **Step 4: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Start local dev server for smoke**

Run:

```bash
uv run python -m jarvis serve --config-dir config --db-url sqlite+aiosqlite:///./data/jarvis.db
```

Expected: server starts and prints `jarvis serving on http://0.0.0.0:8080`.

- [ ] **Step 6: Browser smoke**

Open these pages in the in-app Browser:

```text
http://localhost:8080/memory
http://localhost:8080/conversations
http://localhost:8080/actions
http://localhost:8080/mcp
http://localhost:8080/settings
```

Expected:

- `/memory` renders without template errors and shows Preferences and Recall Memories sections.
- `/conversations` renders and conversation detail pages show a Recalled memories section.
- Existing `/actions`, `/mcp`, and `/settings` pages still render.

- [ ] **Step 7: Stop dev server**

Stop the server session with Ctrl-C. Confirm the terminal returns to the shell prompt.

- [ ] **Step 8: Commit**

Run:

```bash
git add README.md
git commit -m "docs: document memory preferences"
```

- [ ] **Step 9: Final branch check**

Run:

```bash
git status --short
git log --oneline -8
```

Expected: working tree clean and the recent commits are the memory feature commits.

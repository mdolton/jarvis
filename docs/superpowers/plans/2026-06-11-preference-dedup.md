# Preference Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop near-duplicate memory *preferences* from accumulating — drop semantic duplicates before they reach the Memory tab, and add an on-demand "Find duplicates" dashboard tool to clean up existing ones.

**Architecture:** Each preference gets an embedding stored in a new nullable column on `memory_preferences`. A `PreferenceDeduplicator` compares a candidate against existing preferences using cosine similarity; matches above a high threshold are duplicates outright, matches in an ambiguous band are confirmed by an LLM judge. The deduplicator is injected into `MemoryService`, runs inside the existing `summarize_run` proposal path (best-effort, falling back to today's exact-match dedup on any failure), and powers a `find_duplicate_preferences()` method behind a new dashboard button.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (async) + Alembic, SQLite, FastAPI + Jinja2, OpenAI-compatible client (`AsyncOpenAI`), pytest (async).

**Spec:** `docs/superpowers/specs/2026-06-11-preference-dedup-design.md`

---

## File Structure

**Create:**
- `jarvis/memory/preference_dedup.py` — `cosine`, dataclasses (`ExistingPreference`, `ClusterPreference`, `DuplicateMatch`), `PreferenceJudge`, `PreferenceDeduplicator`, `choose_keeper`.
- `alembic/versions/0010_preference_embeddings.py` — migration adding `embedding` + `embedding_dimensions` columns.
- `tests/unit/test_preference_dedup.py` — unit tests for the deduplicator.

**Modify:**
- `jarvis/config/schema.py` — new `MemoryConfig` fields.
- `config/jarvis.yaml.example` — document new fields.
- `jarvis/persistence/models.py` — two new columns on `MemoryPreferenceRow`.
- `jarvis/persistence/repositories.py` — `NewPreference` dataclass; `create_pending`/`create_pending_many`/`_create_missing_pending` accept embeddings; add `list_for_dedup` and `set_embedding`.
- `jarvis/core/types.py` — two new `AuditEventType` members.
- `jarvis/memory/service.py` — inject deduplicator; rewrite `_create_preference_proposals`; emit drop/skip audit events; add `find_duplicate_preferences`.
- `jarvis/main.py` — construct `PreferenceJudge` + `PreferenceDeduplicator`, pass into `MemoryService`.
- `jarvis/web/routes/memory.py` — `POST /memory/preferences/find-duplicates` route.
- `jarvis/web/templates/memory.html` — "Find duplicates" button + duplicate-clusters section.
- `tests/unit/test_config_schema.py`, `tests/integration/test_memory_migration.py`, `tests/integration/test_memory_service.py`, `tests/integration/test_repositories_memory.py` — extended tests.

---

## Task 1: Config fields

**Files:**
- Modify: `jarvis/config/schema.py:26-32`
- Modify: `config/jarvis.yaml.example:24` (memory block)
- Test: `tests/unit/test_config_schema.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_config_schema.py`:

```python
def test_memory_config_preference_dedup_defaults():
    from jarvis.config.schema import MemoryConfig

    cfg = MemoryConfig()

    assert cfg.preference_dedup_enabled is True
    assert cfg.preference_dup_high_threshold == 0.92
    assert cfg.preference_dup_low_threshold == 0.82
    assert cfg.preference_dedup_max_judge_calls == 5


def test_memory_config_rejects_out_of_range_threshold():
    import pytest
    from pydantic import ValidationError

    from jarvis.config.schema import MemoryConfig

    with pytest.raises(ValidationError):
        MemoryConfig(preference_dup_high_threshold=1.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config_schema.py -k preference_dedup -v`
Expected: FAIL (`AttributeError` / unexpected keyword — fields don't exist yet).

- [ ] **Step 3: Add the fields**

In `jarvis/config/schema.py`, extend `MemoryConfig`:

```python
class MemoryConfig(_StrictModel):
    enabled: bool = True
    recall_enabled: bool = True
    embedding_model: str | None = None
    embedding_dimensions: int = Field(default=1536, ge=1)
    max_recalled_memories: int = Field(default=5, ge=0, le=20)
    min_relevance_score: float = Field(default=0.25, ge=0.0)
    preference_dedup_enabled: bool = True
    preference_dup_high_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    preference_dup_low_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    preference_dedup_max_judge_calls: int = Field(default=5, ge=0)
```

- [ ] **Step 4: Document in the example config**

In `config/jarvis.yaml.example`, under the `memory:` block (after `min_relevance_score`), add:

```yaml
  # Deduplicate proposed preferences semantically (embedding + LLM tiebreak).
  preference_dedup_enabled: true
  # Cosine >= high -> duplicate outright; in [low, high) -> ask the LLM judge.
  preference_dup_high_threshold: 0.92
  preference_dup_low_threshold: 0.82
  # Cap LLM judge calls per proposal batch / per Find-duplicates run.
  preference_dedup_max_judge_calls: 5
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config_schema.py -k preference_dedup -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add jarvis/config/schema.py config/jarvis.yaml.example tests/unit/test_config_schema.py
git commit -m "feat(memory): add preference dedup config fields"
```

---

## Task 2: DB columns + migration

**Files:**
- Modify: `jarvis/persistence/models.py:107-127`
- Create: `alembic/versions/0010_preference_embeddings.py`
- Test: `tests/integration/test_memory_migration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_memory_migration.py` (mirrors the existing `_run_alembic` helper and column-introspection style in that file):

```python
def test_memory_migration_adds_preference_embedding_columns(tmp_path):
    import sqlite3

    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr

    with sqlite3.connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info('memory_preferences')").fetchall()
        }
    assert "embedding" in cols
    assert "embedding_dimensions" in cols

    down = _run_alembic(db_path, "downgrade 0009")
    assert down.returncode == 0, down.stderr
    with sqlite3.connect(db_path) as conn:
        cols_after = {
            row[1]
            for row in conn.execute("PRAGMA table_info('memory_preferences')").fetchall()
        }
    assert "embedding" not in cols_after
    assert "embedding_dimensions" not in cols_after
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_memory_migration.py -k embedding -v`
Expected: FAIL (`embedding` not in columns — migration doesn't exist).

- [ ] **Step 3: Add the columns to the model**

In `jarvis/persistence/models.py`, add to `MemoryPreferenceRow` (after `archived_at`, before `__table_args__`):

```python
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

Confirm `JSON` and `Integer` are imported at the top of the file (they are used elsewhere in this module — add `Integer` to the `from sqlalchemy import ...` line if missing).

- [ ] **Step 4: Create the migration**

Create `alembic/versions/0010_preference_embeddings.py`:

```python
"""add preference embedding columns

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_preferences",
        sa.Column("embedding", sa.JSON(), nullable=True),
    )
    op.add_column(
        "memory_preferences",
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_preferences", "embedding_dimensions")
    op.drop_column("memory_preferences", "embedding")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_memory_migration.py -v`
Expected: PASS (new test + existing migration tests still green).

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/models.py alembic/versions/0010_preference_embeddings.py tests/integration/test_memory_migration.py
git commit -m "feat(memory): add embedding columns to memory_preferences"
```

---

## Task 3: Repository support for embeddings

**Files:**
- Modify: `jarvis/persistence/repositories.py` (`MemoryPreferenceRepo`, lines ~234-403; helper `_normalize_preference_content` at ~643)
- Test: `tests/integration/test_repositories_memory.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_repositories_memory.py` (use the same engine/session fixtures already in that file; if the file uses a `factory` fixture, follow it — otherwise copy the session setup from a neighboring test):

```python
import pytest

from jarvis.persistence.repositories import MemoryPreferenceRepo, NewPreference


@pytest.mark.asyncio
async def test_create_pending_many_persists_embeddings(factory):
    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        rows = await repo.create_pending_many(
            items=[
                NewPreference(content="Run tests first", embedding=[0.1, 0.2], embedding_dimensions=2),
                NewPreference(content="Use tabs"),
            ],
            source="agent_proposal",
        )
    assert len(rows) == 2
    by_content = {r.content: r for r in rows}
    assert by_content["Run tests first"].embedding == [0.1, 0.2]
    assert by_content["Run tests first"].embedding_dimensions == 2
    assert by_content["Use tabs"].embedding is None


@pytest.mark.asyncio
async def test_list_for_dedup_excludes_archived(factory):
    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        keep = await repo.create_pending(content="Keep me", source="agent_proposal")
        drop = await repo.create_pending(content="Archive me", source="agent_proposal")
        await repo.archive(drop.id)
    async with factory() as session:
        rows = await MemoryPreferenceRepo(session).list_for_dedup()
    contents = {r.content for r in rows}
    assert "Keep me" in contents
    assert "Archive me" not in contents


@pytest.mark.asyncio
async def test_set_embedding_updates_row(factory):
    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        row = await repo.create_pending(content="No embedding yet", source="agent_proposal")
    async with factory() as session:
        await MemoryPreferenceRepo(session).set_embedding(row.id, [0.5, 0.6, 0.7], 3)
    async with factory() as session:
        refreshed = await MemoryPreferenceRepo(session).get_by_normalized_content(
            __import__("re").sub(r"\s+", " ", "No embedding yet").strip().casefold()
        )
    assert refreshed.embedding == [0.5, 0.6, 0.7]
    assert refreshed.embedding_dimensions == 3
```

> If `test_repositories_memory.py` does not already define a `factory` fixture, add one at the top of the file copying the engine/session setup used by the other integration tests (see `tests/integration/test_memory_service.py` for the pattern: `create_engine` → `Base.metadata.create_all` → `session_factory(engine)`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_repositories_memory.py -k "embedding or dedup" -v`
Expected: FAIL (`ImportError: cannot import name 'NewPreference'`).

- [ ] **Step 3: Add the `NewPreference` dataclass**

Near the top of `jarvis/persistence/repositories.py` (after imports, before the first repo class), add:

```python
@dataclass(frozen=True, slots=True)
class NewPreference:
    content: str
    embedding: list[float] | None = None
    embedding_dimensions: int | None = None
```

Ensure `from dataclasses import dataclass` is imported at the top of the file (add it if absent).

- [ ] **Step 4: Update `create_pending` to accept an embedding**

Replace `MemoryPreferenceRepo.create_pending` (lines ~238-259) with:

```python
    async def create_pending(
        self,
        *,
        content: str,
        source: str,
        embedding: list[float] | None = None,
        embedding_dimensions: int | None = None,
    ) -> MemoryPreferenceRow:
        now = _utcnow()
        content_normalized = _normalize_preference_content(content)
        row = MemoryPreferenceRow(
            content=content,
            content_normalized=content_normalized,
            status="pending",
            source=source,
            embedding=embedding,
            embedding_dimensions=embedding_dimensions,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing = await self.get_by_normalized_content(content_normalized)
            if existing is None:
                raise
            return existing
        await self._session.refresh(row)
        return row
```

- [ ] **Step 5: Update `create_pending_many` and `_create_missing_pending` to take `NewPreference` items**

Replace `create_pending_many` (lines ~261-295) and `_create_missing_pending` (lines ~297-334) with:

```python
    async def create_pending_many(
        self,
        *,
        items: list[NewPreference],
        source: str,
    ) -> list[MemoryPreferenceRow]:
        if not items:
            return []
        now = _utcnow()
        rows = [
            MemoryPreferenceRow(
                content=item.content,
                content_normalized=_normalize_preference_content(item.content),
                status="pending",
                source=source,
                embedding=item.embedding,
                embedding_dimensions=item.embedding_dimensions,
                created_at=now,
                updated_at=now,
            )
            for item in items
        ]
        try:
            self._session.add_all(rows)
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return await self._create_missing_pending(items=items, source=source)
        except Exception:
            await self._session.rollback()
            raise
        for row in rows:
            await self._session.refresh(row)
        return rows

    async def _create_missing_pending(
        self,
        *,
        items: list[NewPreference],
        source: str,
    ) -> list[MemoryPreferenceRow]:
        existing = await self.existing_normalized_contents()
        missing = [
            item
            for item in items
            if _normalize_preference_content(item.content) not in existing
        ]
        if not missing:
            return []
        now = _utcnow()
        rows = [
            MemoryPreferenceRow(
                content=item.content,
                content_normalized=_normalize_preference_content(item.content),
                status="pending",
                source=source,
                embedding=item.embedding,
                embedding_dimensions=item.embedding_dimensions,
                created_at=now,
                updated_at=now,
            )
            for item in missing
        ]
        try:
            self._session.add_all(rows)
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return []
        except Exception:
            await self._session.rollback()
            raise
        for row in rows:
            await self._session.refresh(row)
        return rows
```

- [ ] **Step 6: Add `list_for_dedup` and `set_embedding`**

In `MemoryPreferenceRepo`, after `list_for_dashboard` (line ~368), add:

```python
    async def list_for_dedup(self) -> list[MemoryPreferenceRow]:
        result = await self._session.execute(
            select(MemoryPreferenceRow)
            .where(MemoryPreferenceRow.status != "archived")
            .order_by(MemoryPreferenceRow.created_at.asc())
        )
        return list(result.scalars())

    async def set_embedding(
        self,
        preference_id: UUID,
        embedding: list[float],
        embedding_dimensions: int,
    ) -> None:
        row = await self._session.get(MemoryPreferenceRow, preference_id)
        if row is None:
            return
        row.embedding = embedding
        row.embedding_dimensions = embedding_dimensions
        row.updated_at = _utcnow()
        await self._session.commit()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_repositories_memory.py -v`
Expected: PASS (new tests + existing ones still green).

- [ ] **Step 8: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_memory.py
git commit -m "feat(memory): repo support for preference embeddings"
```

---

## Task 4: cosine helper + PreferenceJudge

**Files:**
- Create: `jarvis/memory/preference_dedup.py`
- Test: `tests/unit/test_preference_dedup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_preference_dedup.py`:

```python
import math
from types import SimpleNamespace

import pytest

from jarvis.memory.preference_dedup import PreferenceJudge, cosine


def test_cosine_identical_is_one():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_mismatched_or_empty_is_zero():
    assert cosine([1.0, 0.0], [1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


class _FakeChat:
    def __init__(self, content):
        self._content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class _FakeClient:
    def __init__(self, content):
        self.chat = SimpleNamespace(completions=_FakeChat(content))


@pytest.mark.asyncio
async def test_judge_parses_true():
    client = _FakeClient('{"duplicate": true}')
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is True


@pytest.mark.asyncio
async def test_judge_parses_fenced_false():
    client = _FakeClient('```json\n{"duplicate": false}\n```')
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is False


@pytest.mark.asyncio
async def test_judge_returns_false_on_garbage():
    client = _FakeClient("not json at all")
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is False


@pytest.mark.asyncio
async def test_judge_returns_false_on_error():
    class _Boom:
        async def create(self, **kwargs):
            raise RuntimeError("boom")

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Boom()))
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_preference_dedup.py -v`
Expected: FAIL (`ModuleNotFoundError: jarvis.memory.preference_dedup`).

- [ ] **Step 3: Create the module with `cosine` + `PreferenceJudge`**

Create `jarvis/memory/preference_dedup.py`:

```python
from __future__ import annotations

import json
import math
from json import JSONDecodeError
from typing import Protocol


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class JudgeProtocol(Protocol):
    async def judge(self, *, candidate: str, existing: str) -> bool: ...


class PreferenceJudge:
    """LLM tiebreak: is CANDIDATE already covered by EXISTING?"""

    def __init__(self, *, client, model: str) -> None:
        self._client = client
        self._model = model

    async def judge(self, *, candidate: str, existing: str) -> bool:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You deduplicate behavioral preference rules for an AI "
                            "assistant. Decide whether the CANDIDATE preference is "
                            "already covered by the EXISTING preference - i.e. the "
                            "same instruction (even if worded differently) or a "
                            "strict subset of it. Return strict JSON: "
                            '{"duplicate": true} or {"duplicate": false}.'
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"EXISTING:\n{existing}\n\nCANDIDATE:\n{candidate}",
                    },
                ],
            )
        except Exception:
            return False
        return _parse_duplicate(_message_content(response))


def _message_content(response: object) -> str | None:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", None)


def _parse_duplicate(text: str | None) -> bool:
    if not text:
        return False
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return False
    try:
        data = json.loads(cleaned[start : end + 1])
    except (JSONDecodeError, ValueError):
        return False
    return bool(data.get("duplicate")) if isinstance(data, dict) else False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_preference_dedup.py -v`
Expected: PASS (all judge + cosine tests).

- [ ] **Step 5: Commit**

```bash
git add jarvis/memory/preference_dedup.py tests/unit/test_preference_dedup.py
git commit -m "feat(memory): add cosine helper and preference LLM judge"
```

---

## Task 5: PreferenceDeduplicator.is_duplicate

**Files:**
- Modify: `jarvis/memory/preference_dedup.py`
- Test: `tests/unit/test_preference_dedup.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_preference_dedup.py`:

```python
from jarvis.memory.preference_dedup import (
    ExistingPreference,
    PreferenceDeduplicator,
)


class _RecordingJudge:
    def __init__(self, verdict: bool):
        self._verdict = verdict
        self.calls = 0

    async def judge(self, *, candidate: str, existing: str) -> bool:
        self.calls += 1
        return self._verdict


class _StubEmbeddings:
    def __init__(self, vector):
        self._vector = vector

    async def embed(self, text: str) -> list[float]:
        return list(self._vector)


def _dedup(judge, *, high=0.92, low=0.82):
    return PreferenceDeduplicator(
        embedding_provider=_StubEmbeddings([0.0]),
        judge=judge,
        high_threshold=high,
        low_threshold=low,
        max_judge_calls=5,
    )


@pytest.mark.asyncio
async def test_is_duplicate_high_similarity_no_judge():
    judge = _RecordingJudge(False)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="Run tests", embedding=[1.0, 0.0], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="Run the tests",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is not None
    assert match.method == "embedding"
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_is_duplicate_band_consults_judge_yes():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    # cosine ~0.857, inside [0.82, 0.92)
    existing = [ExistingPreference(content="Run tests", embedding=[1.0, 0.6], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is not None
    assert match.method == "llm"
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_is_duplicate_band_judge_no_keeps():
    judge = _RecordingJudge(False)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="Run tests", embedding=[1.0, 0.6], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is None
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_is_duplicate_below_low_threshold_keeps():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="x", embedding=[0.0, 1.0], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is None
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_is_duplicate_skips_dimension_mismatch():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="x", embedding=[1.0, 0.0, 0.0], embedding_dimensions=3, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is None


@pytest.mark.asyncio
async def test_is_duplicate_none_candidate_embedding_keeps():
    dedup = _dedup(_RecordingJudge(True))
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=None,
        existing=[ExistingPreference(content="x", embedding=[1.0, 0.0], embedding_dimensions=2, status="active", preference_id=None)],
        judge_budget=dedup.new_budget(),
    )
    assert match is None


@pytest.mark.asyncio
async def test_judge_budget_caps_calls():
    judge = _RecordingJudge(False)
    dedup = PreferenceDeduplicator(
        embedding_provider=_StubEmbeddings([0.0]),
        judge=judge,
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=1,
    )
    budget = dedup.new_budget()
    existing = [ExistingPreference(content="x", embedding=[1.0, 0.6], embedding_dimensions=2, status="active", preference_id=None)]
    await dedup.is_duplicate(candidate_content="c1", candidate_embedding=[1.0, 0.0], existing=existing, judge_budget=budget)
    await dedup.is_duplicate(candidate_content="c2", candidate_embedding=[1.0, 0.0], existing=existing, judge_budget=budget)
    assert judge.calls == 1  # second call had no budget left
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_preference_dedup.py -k is_duplicate -v`
Expected: FAIL (`ImportError: cannot import name 'ExistingPreference'` / `PreferenceDeduplicator`).

- [ ] **Step 3: Add the dataclasses, budget, and deduplicator**

Append to `jarvis/memory/preference_dedup.py` (after the existing code; add `from dataclasses import dataclass`, `from datetime import datetime`, and `from uuid import UUID` to the imports at the top):

```python
@dataclass(frozen=True, slots=True)
class ExistingPreference:
    content: str
    embedding: list[float] | None
    embedding_dimensions: int | None
    status: str
    preference_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    matched_content: str
    matched_id: UUID | None
    score: float
    method: str  # "embedding" | "llm"


class _JudgeBudget:
    def __init__(self, limit: int) -> None:
        self._remaining = limit

    def available(self) -> bool:
        return self._remaining > 0

    def consume(self) -> None:
        self._remaining -= 1


class PreferenceDeduplicator:
    def __init__(
        self,
        *,
        embedding_provider,
        judge: JudgeProtocol,
        high_threshold: float,
        low_threshold: float,
        max_judge_calls: int,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._judge = judge
        self._high_threshold = high_threshold
        self._low_threshold = low_threshold
        self._max_judge_calls = max_judge_calls

    def new_budget(self) -> _JudgeBudget:
        return _JudgeBudget(self._max_judge_calls)

    async def embed(self, content: str) -> list[float] | None:
        try:
            return await self._embedding_provider.embed(content)
        except Exception:
            return None

    async def is_duplicate(
        self,
        *,
        candidate_content: str,
        candidate_embedding: list[float] | None,
        existing: list[ExistingPreference],
        judge_budget: _JudgeBudget,
    ) -> DuplicateMatch | None:
        if not candidate_embedding:
            return None
        best_score = -1.0
        best_pref: ExistingPreference | None = None
        for pref in existing:
            if not pref.embedding:
                continue
            if pref.embedding_dimensions != len(candidate_embedding):
                continue
            score = cosine(candidate_embedding, pref.embedding)
            if score > best_score:
                best_score = score
                best_pref = pref
        if best_pref is None:
            return None
        if best_score >= self._high_threshold:
            return DuplicateMatch(best_pref.content, best_pref.preference_id, best_score, "embedding")
        if best_score >= self._low_threshold and judge_budget.available():
            judge_budget.consume()
            if await self._judge.judge(candidate=candidate_content, existing=best_pref.content):
                return DuplicateMatch(best_pref.content, best_pref.preference_id, best_score, "llm")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_preference_dedup.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add jarvis/memory/preference_dedup.py tests/unit/test_preference_dedup.py
git commit -m "feat(memory): add PreferenceDeduplicator.is_duplicate"
```

---

## Task 6: PreferenceDeduplicator.cluster + choose_keeper

**Files:**
- Modify: `jarvis/memory/preference_dedup.py`
- Test: `tests/unit/test_preference_dedup.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_preference_dedup.py`:

```python
from datetime import UTC, datetime
from uuid import uuid4

from jarvis.memory.preference_dedup import ClusterPreference, choose_keeper


def _cp(content, vec, status="pending", created=1, updated=1):
    base = datetime(2026, 6, 1, tzinfo=UTC)
    return ClusterPreference(
        preference_id=uuid4(),
        content=content,
        status=status,
        created_at=base.replace(day=created),
        updated_at=base.replace(day=updated),
        embedding=vec,
        embedding_dimensions=len(vec) if vec else None,
    )


@pytest.mark.asyncio
async def test_cluster_groups_high_similarity_pairs():
    judge = _RecordingJudge(False)
    dedup = _dedup(judge)
    prefs = [
        _cp("Run tests", [1.0, 0.0]),
        _cp("Run the tests", [1.0, 0.0]),
        _cp("Use dark mode", [0.0, 1.0]),
    ]
    groups = await dedup.cluster(prefs)
    assert len(groups) == 1
    assert {p.content for p in groups[0]} == {"Run tests", "Run the tests"}
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_cluster_uses_judge_in_band():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    prefs = [
        _cp("a", [1.0, 0.0]),
        _cp("b", [1.0, 0.6]),  # cosine ~0.857, in band
    ]
    groups = await dedup.cluster(prefs)
    assert len(groups) == 1
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_cluster_skips_dimension_mismatch():
    dedup = _dedup(_RecordingJudge(True))
    groups = await dedup.cluster([
        _cp("a", [1.0, 0.0]),
        _cp("b", [1.0, 0.0, 0.0]),
    ])
    assert groups == []


def test_choose_keeper_prefers_oldest_active():
    active_new = _cp("new", [1.0], status="active", created=5)
    active_old = _cp("old", [1.0], status="active", created=2)
    pending = _cp("pending", [1.0], status="pending", created=1)
    assert choose_keeper([active_new, active_old, pending]) is active_old


def test_choose_keeper_falls_back_to_most_recent_update():
    p1 = _cp("a", [1.0], status="pending", updated=2)
    p2 = _cp("b", [1.0], status="rejected", updated=9)
    assert choose_keeper([p1, p2]) is p2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_preference_dedup.py -k "cluster or keeper" -v`
Expected: FAIL (`ImportError: cannot import name 'ClusterPreference'`).

- [ ] **Step 3: Add `ClusterPreference`, `cluster`, and `choose_keeper`**

Append to `jarvis/memory/preference_dedup.py`. Add the dataclass near the other dataclasses:

```python
@dataclass(frozen=True, slots=True)
class ClusterPreference:
    preference_id: UUID
    content: str
    status: str
    created_at: datetime
    updated_at: datetime
    embedding: list[float] | None
    embedding_dimensions: int | None
```

Add the `cluster` method to `PreferenceDeduplicator`:

```python
    async def cluster(
        self, preferences: list[ClusterPreference]
    ) -> list[list[ClusterPreference]]:
        n = len(preferences)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        budget = self.new_budget()
        for i in range(n):
            for j in range(i + 1, n):
                a, b = preferences[i], preferences[j]
                if not a.embedding or not b.embedding:
                    continue
                if a.embedding_dimensions != b.embedding_dimensions:
                    continue
                score = cosine(a.embedding, b.embedding)
                connected = False
                if score >= self._high_threshold:
                    connected = True
                elif score >= self._low_threshold and budget.available():
                    budget.consume()
                    connected = await self._judge.judge(candidate=a.content, existing=b.content)
                if connected:
                    union(i, j)

        groups: dict[int, list[ClusterPreference]] = {}
        for idx in range(n):
            groups.setdefault(find(idx), []).append(preferences[idx])
        return [group for group in groups.values() if len(group) >= 2]
```

Add the module-level helper:

```python
def choose_keeper(group: list[ClusterPreference]) -> ClusterPreference:
    actives = [p for p in group if p.status == "active"]
    if actives:
        return min(actives, key=lambda p: p.created_at)
    return max(group, key=lambda p: p.updated_at)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_preference_dedup.py -v`
Expected: PASS (entire file).

- [ ] **Step 5: Commit**

```bash
git add jarvis/memory/preference_dedup.py tests/unit/test_preference_dedup.py
git commit -m "feat(memory): add preference clustering and keeper selection"
```

---

## Task 7: Audit events + wire dedup into the service

**Files:**
- Modify: `jarvis/core/types.py:63-68`
- Modify: `jarvis/memory/service.py` (constructor ~36-54; `summarize_run` proposal call ~157-161; emit block ~182-190; `_create_preference_proposals` ~540-557; add `find_duplicate_preferences`)
- Test: `tests/integration/test_memory_service.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_memory_service.py`. First, two fakes near the top of the file (after the existing `FakeEmbeddingProvider`):

```python
class FakeSummarizer:
    def __init__(self, preference_candidates):
        self._candidates = preference_candidates

    async def summarize(self, *, user_prompt, assistant_output):
        from jarvis.memory.types import MemorySummary

        return MemorySummary(
            summary="did a thing",
            topics=[],
            entities=[],
            evidence=[],
            preference_candidates=list(self._candidates),
        )


class SequencedEmbeddingProvider:
    """Returns a preset vector per exact text, else a default."""

    def __init__(self, mapping, default=None):
        self._mapping = mapping
        self._default = default or [0.0, 0.0]

    async def embed(self, text: str) -> list[float]:
        return list(self._mapping.get(text, self._default))
```

Then the tests:

```python
@pytest.mark.asyncio
async def test_summarize_run_drops_semantic_duplicate_of_active(factory):
    from jarvis.memory.preference_dedup import PreferenceDeduplicator
    from jarvis.persistence.repositories import MemoryPreferenceRepo

    # seed an approved (active) preference with an embedding
    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        pref = await repo.create_pending(
            content="Always run the tests before committing",
            source="agent_proposal",
            embedding=[1.0, 0.0],
            embedding_dimensions=2,
        )
        await repo.approve(pref.id)

    class _NoJudge:
        async def judge(self, *, candidate, existing):
            return False

    embeddings = SequencedEmbeddingProvider(
        {"Run the test suite before each commit": [1.0, 0.0]}  # high-similarity dup
    )
    dedup = PreferenceDeduplicator(
        embedding_provider=embeddings,
        judge=_NoJudge(),
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=5,
    )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=embeddings,
        vector_store=FakeVectorStore(uuid4(), available=False),
        max_recalled_memories=0,
        min_relevance_score=0.25,
        summarizer=FakeSummarizer(["Run the test suite before each commit"]),
        preference_deduplicator=dedup,
    )

    outcome = await service.summarize_run(
        conversation_id=None,
        channel_kind="discord",
        channel_ref="c1",
        user_prompt="u",
        assistant_output="a",
    )
    assert outcome.preferences_created == 0


@pytest.mark.asyncio
async def test_summarize_run_keeps_distinct_preference(factory):
    from jarvis.memory.preference_dedup import PreferenceDeduplicator
    from jarvis.persistence.repositories import MemoryPreferenceRepo

    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        pref = await repo.create_pending(
            content="Always run the tests before committing",
            source="agent_proposal",
            embedding=[1.0, 0.0],
            embedding_dimensions=2,
        )
        await repo.approve(pref.id)

    class _NoJudge:
        async def judge(self, *, candidate, existing):
            return False

    embeddings = SequencedEmbeddingProvider({"Use dark mode in all dashboards": [0.0, 1.0]})
    dedup = PreferenceDeduplicator(
        embedding_provider=embeddings,
        judge=_NoJudge(),
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=5,
    )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=embeddings,
        vector_store=FakeVectorStore(uuid4(), available=False),
        max_recalled_memories=0,
        min_relevance_score=0.25,
        summarizer=FakeSummarizer(["Use dark mode in all dashboards"]),
        preference_deduplicator=dedup,
    )

    outcome = await service.summarize_run(
        conversation_id=None, channel_kind="discord", channel_ref="c1",
        user_prompt="u", assistant_output="a",
    )
    assert outcome.preferences_created == 1


@pytest.mark.asyncio
async def test_summarize_run_falls_back_when_dedup_embed_fails(factory):
    from jarvis.memory.preference_dedup import PreferenceDeduplicator

    class _BrokenEmbeddings:
        async def embed(self, text: str):
            raise RuntimeError("embed down")

    class _NoJudge:
        async def judge(self, *, candidate, existing):
            return False

    dedup = PreferenceDeduplicator(
        embedding_provider=_BrokenEmbeddings(),
        judge=_NoJudge(),
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=5,
    )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(uuid4(), available=False),
        max_recalled_memories=0,
        min_relevance_score=0.25,
        summarizer=FakeSummarizer(["Brand new rule about formatting"]),
        preference_deduplicator=dedup,
    )

    outcome = await service.summarize_run(
        conversation_id=None, channel_kind="discord", channel_ref="c1",
        user_prompt="u", assistant_output="a",
    )
    # embed() returning None for the candidate means no comparison -> candidate kept
    assert outcome.preferences_created == 1
```

> Note: `embed()` swallows exceptions and returns `None` (Task 5), so a broken embedder yields `candidate_embedding=None` → `is_duplicate` returns `None` → the candidate is proposed. This is the conservative "propose when uncertain" behavior. The hard-failure fallback path (whole semantic pass raising) is covered by the `try/except` in Step 4.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_memory_service.py -k "summarize_run" -v`
Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'preference_deduplicator'`).

- [ ] **Step 3: Add the audit event types**

In `jarvis/core/types.py`, in the `AuditEventType` enum after `MEMORY_FAILED`:

```python
    MEMORY_PREFERENCE_DEDUP_DROPPED = "memory.preference_dedup_dropped"
    MEMORY_PREFERENCE_DEDUP_SKIPPED = "memory.preference_dedup_skipped"
```

- [ ] **Step 4: Add the deduplicator to the service and rewrite the proposal flow**

In `jarvis/memory/service.py`:

(a) Constructor — add the parameter and store it:

```python
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        vector_store: MemoryVectorStore,
        max_recalled_memories: int,
        min_relevance_score: float,
        summarizer=None,
        audit=None,
        preference_deduplicator=None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._summarizer = summarizer
        self._audit = audit
        self._preference_deduplicator = preference_deduplicator
        self._max_recalled_memories = max_recalled_memories
        self._min_relevance_score = min_relevance_score
```

(b) Add imports at the top of the file:

```python
from jarvis.memory.preference_dedup import (
    ClusterPreference,
    DuplicateMatch,
    ExistingPreference,
    choose_keeper,
)
from jarvis.persistence.repositories import (
    MemoryEntryRepo,
    MemoryPreferenceRepo,
    MemoryRecallRepo,
    NewPreference,
)
```

(replace the existing `from jarvis.persistence.repositories import ...` line).

(c) Add a result dataclass near `MemorySummarizeOutcome` (after line ~33):

```python
@dataclass(frozen=True, slots=True)
class _ProposalResult:
    created: list[MemoryPreferenceRow]
    dropped: list[DuplicateMatch]
    fell_back: bool
```

(d) Replace the proposal call site in `summarize_run` (lines ~157-161) with:

```python
                proposal_result = await _create_preference_proposals(
                    session,
                    summary.preference_candidates,
                    self._preference_deduplicator,
                )
                created_preferences = proposal_result.created
                preferences_created = len(created_preferences)
```

(e) After the existing `MEMORY_PREFERENCE_PROPOSED` emit loop (after line ~190), add drop/skip emits:

```python
        for dropped in proposal_result.dropped:
            await self._emit(
                AuditEventType.MEMORY_PREFERENCE_DEDUP_DROPPED,
                conversation_id=conversation_id,
                payload={
                    "matched_preference_id": str(dropped.matched_id) if dropped.matched_id else None,
                    "matched_content": dropped.matched_content,
                    "score": dropped.score,
                    "method": dropped.method,
                },
            )
        if proposal_result.fell_back:
            await self._emit(
                AuditEventType.MEMORY_PREFERENCE_DEDUP_SKIPPED,
                conversation_id=conversation_id,
                payload={"reason": "dedup pass failed; used exact-match fallback"},
            )
```

> `proposal_result` is defined inside the `async with self._session_factory() as session:` block. Confirm the emit block above sits at the same indentation level as the existing `for preference in created_preferences:` loop (outside the session block, inside `summarize_run`). If `proposal_result` is not in scope there because of an early `return` on exception, initialise `proposal_result = _ProposalResult([], [], False)` before the `try:` at line ~123.

(f) Replace `_create_preference_proposals` (lines ~540-557) with:

```python
async def _create_preference_proposals(
    session: AsyncSession,
    candidates: list[str],
    deduplicator,
) -> _ProposalResult:
    existing_norm = await _existing_preferences(session)
    limited_candidates = _limited_unique_preference_candidates(candidates)
    surviving = [
        (normalized, content)
        for normalized, content in limited_candidates
        if normalized not in existing_norm
    ]
    if not surviving:
        return _ProposalResult([], [], False)

    repo = MemoryPreferenceRepo(session)

    if deduplicator is None:
        created = await repo.create_pending_many(
            items=[NewPreference(content=content) for _, content in surviving],
            source="agent_proposal",
        )
        return _ProposalResult(created, [], False)

    try:
        existing_rows = await repo.list_for_dedup()
        comparison_set = await _ensure_dedup_embeddings(session, repo, deduplicator, existing_rows)
        budget = deduplicator.new_budget()
        accepted: list[NewPreference] = []
        dropped: list[DuplicateMatch] = []
        for _, content in surviving:
            embedding = await deduplicator.embed(content)
            match = await deduplicator.is_duplicate(
                candidate_content=content,
                candidate_embedding=embedding,
                existing=comparison_set,
                judge_budget=budget,
            )
            if match is not None:
                dropped.append(match)
                continue
            dims = len(embedding) if embedding else None
            accepted.append(
                NewPreference(content=content, embedding=embedding, embedding_dimensions=dims)
            )
            comparison_set.append(
                ExistingPreference(
                    content=content,
                    embedding=embedding,
                    embedding_dimensions=dims,
                    status="pending",
                    preference_id=None,
                )
            )
        created = await repo.create_pending_many(items=accepted, source="agent_proposal")
        return _ProposalResult(created, dropped, False)
    except Exception:
        created = await repo.create_pending_many(
            items=[NewPreference(content=content) for _, content in surviving],
            source="agent_proposal",
        )
        return _ProposalResult(created, [], True)


async def _ensure_dedup_embeddings(
    session: AsyncSession,
    repo: "MemoryPreferenceRepo",
    deduplicator,
    rows: list[MemoryPreferenceRow],
) -> list[ExistingPreference]:
    result: list[ExistingPreference] = []
    for row in rows:
        embedding = row.embedding
        dims = row.embedding_dimensions
        if embedding is None:
            embedding = await deduplicator.embed(row.content)
            if embedding is not None:
                dims = len(embedding)
                await repo.set_embedding(row.id, embedding, dims)
        result.append(
            ExistingPreference(
                content=row.content,
                embedding=embedding,
                embedding_dimensions=dims,
                status=row.status,
                preference_id=row.id,
            )
        )
    return result
```

(g) Add the `find_duplicate_preferences` method to `MemoryService` (place after `summarize_run`):

```python
    async def find_duplicate_preferences(self) -> list[dict]:
        if self._preference_deduplicator is None:
            return []
        async with self._session_factory() as session:
            repo = MemoryPreferenceRepo(session)
            rows = await repo.list_for_dedup()
            cluster_prefs: list[ClusterPreference] = []
            for row in rows:
                embedding = row.embedding
                dims = row.embedding_dimensions
                if embedding is None:
                    embedding = await self._preference_deduplicator.embed(row.content)
                    if embedding is not None:
                        dims = len(embedding)
                        await repo.set_embedding(row.id, embedding, dims)
                cluster_prefs.append(
                    ClusterPreference(
                        preference_id=row.id,
                        content=row.content,
                        status=row.status,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        embedding=embedding,
                        embedding_dimensions=dims,
                    )
                )
        groups = await self._preference_deduplicator.cluster(cluster_prefs)
        clusters: list[dict] = []
        for group in groups:
            keeper = choose_keeper(group)
            clusters.append(
                {
                    "keeper": keeper,
                    "duplicates": [p for p in group if p.preference_id != keeper.preference_id],
                }
            )
        return clusters
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_memory_service.py -v`
Expected: PASS (new dedup tests + all existing memory-service tests).

- [ ] **Step 6: Commit**

```bash
git add jarvis/core/types.py jarvis/memory/service.py tests/integration/test_memory_service.py
git commit -m "feat(memory): apply semantic dedup to proposed preferences"
```

---

## Task 8: Wire the deduplicator in main.py

**Files:**
- Modify: `jarvis/main.py` (`_build_memory_service`, lines ~258-304)

- [ ] **Step 1: Add the imports**

At the top of `jarvis/main.py`, alongside the other memory imports:

```python
from jarvis.memory.preference_dedup import PreferenceDeduplicator, PreferenceJudge
```

- [ ] **Step 2: Construct and inject the deduplicator**

In `_build_memory_service`, after the `summarizer = MemorySummarizer(...)` block (line ~286-289) and before `service = MemoryService(...)`, add:

```python
    preference_deduplicator = None
    if cfg.jarvis.memory.preference_dedup_enabled:
        preference_deduplicator = PreferenceDeduplicator(
            embedding_provider=embedding_provider,
            judge=PreferenceJudge(client=llm_client, model=cfg.jarvis.llm.model),
            high_threshold=cfg.jarvis.memory.preference_dup_high_threshold,
            low_threshold=cfg.jarvis.memory.preference_dup_low_threshold,
            max_judge_calls=cfg.jarvis.memory.preference_dedup_max_judge_calls,
        )
```

Then add `preference_deduplicator=preference_deduplicator,` to the `MemoryService(...)` constructor call (after `audit=audit,`).

> If `llm_client` is not the variable name in scope here, use whatever `AsyncOpenAI` client the surrounding code passes to `OpenAIEmbeddingProvider(client=...)` (line ~282) — reuse that exact variable.

- [ ] **Step 3: Verify the app builds**

Run: `uv run python -c "import jarvis.main"`
Expected: no import errors.

Run: `uv run pytest tests/ -q`
Expected: PASS (full suite green).

- [ ] **Step 4: Commit**

```bash
git add jarvis/main.py
git commit -m "feat(memory): wire preference deduplicator into app startup"
```

---

## Task 9: "Find duplicates" dashboard tool

**Files:**
- Modify: `jarvis/web/routes/memory.py`
- Modify: `jarvis/web/templates/memory.html`
- Test: `tests/integration/test_web_memory.py` (new file, mirrors the `MagicMock` ctx + `TestClient` pattern in `test_web_conversations.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_web_memory.py`:

```python
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.memory.preference_dedup import ClusterPreference
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


def _cluster_pref(content, status="pending"):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    return ClusterPreference(
        preference_id=uuid4(),
        content=content,
        status=status,
        created_at=now,
        updated_at=now,
        embedding=[1.0, 0.0],
        embedding_dimensions=2,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def memory_client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    keeper = _cluster_pref("Always run tests before committing", status="active")
    dup = _cluster_pref("Run the test suite before each commit")

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.memory_service.find_duplicate_preferences = AsyncMock(
        return_value=[{"keeper": keeper, "duplicates": [dup]}]
    )

    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield client, dup
    await engine.dispose()


def test_find_duplicates_renders_clusters(memory_client):
    client, dup = memory_client
    resp = client.post("/memory/preferences/find-duplicates")
    assert resp.status_code == 200
    assert "Duplicate groups" in resp.text
    assert "Run the test suite before each commit" in resp.text
    # the archive form targets the duplicate, not the keeper
    assert f"/memory/preferences/{dup.preference_id}/archive" in resp.text


def test_memory_page_hides_duplicate_section(memory_client):
    client, _ = memory_client
    resp = client.get("/memory")
    assert resp.status_code == 200
    assert "Duplicate groups" not in resp.text
```

> `ctx` is a `MagicMock`, so `ctx.memory_service` exists and is truthy; we override only `find_duplicate_preferences` with an `AsyncMock`. The real `session_factory` over an empty DB lets `_render_memory_page` read empty preference/entry lists without error.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_web_memory.py -v`
Expected: FAIL (404 — route does not exist).

- [ ] **Step 3: Add the route**

In `jarvis/web/routes/memory.py`, refactor `memory_page` to share rendering and add the new route. Replace the `memory_page` body with a helper call and add the handler:

```python
async def _render_memory_page(request: Request, *, duplicate_clusters=None):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        preference_repo = MemoryPreferenceRepo(session)
        entry_repo = MemoryEntryRepo(session)
        preferences = await preference_repo.list_for_dashboard(limit=100)
        entries = await entry_repo.list_recent(limit=100)
        evidence_by_entry = await entry_repo.list_evidence_for_entries(
            [entry.id for entry in entries]
        )
        entry_items = [
            {"entry": entry, "evidence": evidence_by_entry.get(entry.id, [])}
            for entry in entries
        ]

    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "preferences": preferences,
            "entry_items": entry_items,
            "duplicate_clusters": duplicate_clusters,
        },
    )


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    return await _render_memory_page(request)


@router.post("/memory/preferences/find-duplicates", response_class=HTMLResponse)
async def find_duplicate_preferences(request: Request):
    ctx = request.app.state.ctx
    memory_service = getattr(ctx, "memory_service", None)
    clusters = []
    if memory_service is not None:
        clusters = await memory_service.find_duplicate_preferences()
    return await _render_memory_page(request, duplicate_clusters=clusters)
```

- [ ] **Step 4: Add the button + clusters section to the template**

In `jarvis/web/templates/memory.html`, add a button inside the `Preferences` section header (replace the `<h2>Preferences</h2>` line):

```html
    <div class="section-head-row">
        <h2>Preferences</h2>
        <form class="inline-form" method="post" action="/memory/preferences/find-duplicates">
            <button type="submit">Find duplicates</button>
        </form>
    </div>
```

Then, immediately after the closing `</table>` of the Preferences section (after line ~49, before the `{% if not preferences %}` block is fine too), add the clusters section:

```html
    {% if duplicate_clusters is not none %}
    <h3>Duplicate groups</h3>
    {% if not duplicate_clusters %}
    <p class="muted">No duplicate preferences found.</p>
    {% else %}
    {% for cluster in duplicate_clusters %}
    <div class="dup-cluster">
        <div class="muted">Keep: <strong>{{ cluster.keeper.content }}</strong></div>
        <ul>
            {% for dup in cluster.duplicates %}
            <li>
                {{ dup.content }}
                <form class="inline-form" method="post" action="/memory/preferences/{{ dup.preference_id }}/archive">
                    <button type="submit">Archive</button>
                </form>
            </li>
            {% endfor %}
        </ul>
    </div>
    {% endfor %}
    {% endif %}
    {% endif %}
```

> `duplicate_clusters` defaults to `None` on the GET page (section hidden); the POST handler passes a list (possibly empty) so the section shows with either clusters or the "No duplicate preferences found." message.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_web_memory.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: PASS.

Run: `uv run ruff check jarvis tests`
Expected: no lint errors. Fix any reported.

- [ ] **Step 7: Commit**

```bash
git add jarvis/web/routes/memory.py jarvis/web/templates/memory.html tests/integration/test_web_memory.py
git commit -m "feat(memory): add Find duplicates dashboard tool"
```

---

## Final verification

- [ ] **Run the whole suite + lint:**

Run: `uv run pytest tests/ -q && uv run ruff check jarvis tests`
Expected: all green.

- [ ] **Manual smoke (optional):** start the app (`uv run python -m jarvis serve`), open `/memory`, click **Find duplicates**, confirm the section renders. Approve two near-identical preferences and confirm a later run does not re-propose the same rule.

---

## Notes for the implementer

- **Thresholds (0.92 / 0.82) are starting guesses.** They will need tuning against real embedding output; expect to revisit after observing real proposals. Setting `preference_dedup_enabled: false` restores exact-match-only behavior.
- **Best-effort everywhere:** no embedding or LLM failure may block a proposal or an approval. `embed()` returns `None` on error; the judge returns `False` on error; the whole semantic pass is wrapped in `try/except` that falls back to exact-match.
- **Rejected preferences participate in dedup** (`list_for_dedup` excludes only `archived`), so a previously-rejected rule suppresses re-proposal until it is archived. This is intentional per the spec.
- **Lazy embedding backfill:** the first `summarize_run` and the first **Find duplicates** after deploy will embed existing preferences that lack vectors and persist them; subsequent runs are cheap.

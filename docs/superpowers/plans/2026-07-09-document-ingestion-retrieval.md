# Document Ingestion + Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jarvis answers questions from the user's own content (notes, text files, PDFs) via an ingestion pipeline (chunk → embed → sqlite-vec) and a `search_documents` agent tool.

**Architecture:** Reuse the existing memory stack: generalize `MemoryVectorStore` with a table prefix so document-chunk vectors live in the same SQLite file under `document_*` tables; add `documents` + `document_chunks` ORM tables (migration `0014`) accessed only through a new `DocumentRepo`; a `DocumentService` handles idempotent ingestion (sha256 source-hash skip, like recall-summary dedup) and vector search; a native Agents-SDK `function_tool` exposes retrieval to every agent run (interactive, scheduled, action-resume).

**Tech Stack:** Python 3.12, SQLAlchemy async + SQLite, sqlite-vec, openai-agents SDK (`function_tool`), pypdf (new dep), alembic, pytest (`asyncio_mode=auto`).

## Global Constraints

- Persistence goes through repositories only — never raw sessions in feature code.
- All `Mapped[datetime]` columns use `TZDateTime`; bind timezone-aware UTC datetimes only (repos use `_utcnow()`).
- Migrations that insert UUIDs must use `uuid4().hex`, never `str(uuid4())` (no data inserts planned here, but if any appear).
- Graceful degradation: if sqlite-vec is unavailable, documents are marked `unindexed` and search returns `[]` — mirror `MemoryService` behavior; never raise into the agent run.
- Retrieval tool output stays bounded (≤ ~6000 chars total) to respect runner context budgets.
- Ruff line-length 100, py312. Tests are `async def test_*` with no `@pytest.mark.asyncio`.
- `make check` (ruff + full pytest) green before done. Migration change requires a real-alembic integration test.
- Branch off `main`; commits carry `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Paragraph-aware text chunker

**Files:**
- Create: `jarvis/memory/chunking.py`
- Test: `tests/unit/test_chunking.py`

**Interfaces:**
- Produces: `chunk_text(text: str, *, max_chars: int, overlap: int = 0) -> list[str]` — used by Task 6.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_chunking.py
import pytest

from jarvis.memory.chunking import chunk_text


def test_empty_and_whitespace_text_yields_no_chunks():
    assert chunk_text("", max_chars=100) == []
    assert chunk_text("   \n\n  ", max_chars=100) == []


def test_short_text_is_a_single_chunk():
    assert chunk_text("hello world", max_chars=100) == ["hello world"]


def test_paragraphs_pack_into_chunks_without_splitting():
    text = "para one.\n\npara two.\n\npara three."
    chunks = chunk_text(text, max_chars=25)
    assert chunks == ["para one.\n\npara two.", "para three."]


def test_long_paragraph_is_hard_split_with_overlap():
    text = "abcdefghij" * 10  # 100 chars, no paragraph breaks
    chunks = chunk_text(text, max_chars=40, overlap=10)
    assert all(len(c) <= 40 for c in chunks)
    # step is 30, so consecutive chunks share their 10-char boundary
    assert chunks[0][-10:] == chunks[1][:10]
    # every character is covered
    assert chunks[0] + "".join(c[10:] for c in chunks[1:]) == text


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=0)
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=10, overlap=-1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_chunking.py -q`
Expected: FAIL / error with `ModuleNotFoundError: No module named 'jarvis.memory.chunking'`

- [ ] **Step 3: Implement the chunker**

```python
# jarvis/memory/chunking.py
"""Deterministic paragraph-aware chunking for document ingestion."""

from __future__ import annotations


def chunk_text(text: str, *, max_chars: int, overlap: int = 0) -> list[str]:
    """Split text into chunks of at most ``max_chars``, preferring paragraph breaks.

    Paragraphs (blank-line separated) are packed greedily; a single paragraph
    longer than ``max_chars`` is hard-split with ``overlap`` chars of carryover
    so a fact straddling a cut survives in at least one chunk.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be >= 0 and < max_chars")

    normalized = text.strip()
    if not normalized:
        return []

    paragraphs = [p.strip() for p in normalized.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long(paragraph, max_chars=max_chars, overlap=overlap))
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _split_long(paragraph: str, *, max_chars: int, overlap: int) -> list[str]:
    step = max_chars - overlap
    pieces: list[str] = []
    start = 0
    while start < len(paragraph):
        pieces.append(paragraph[start : start + max_chars])
        if start + max_chars >= len(paragraph):
            break
        start += step
    return pieces
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_chunking.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/memory/chunking.py tests/unit/test_chunking.py
git commit -m "feat: paragraph-aware text chunker for document ingestion"
```

---

### Task 2: Generalize the vector store (table prefix + delete_many)

**Files:**
- Modify: `jarvis/memory/vector_store.py`
- Modify: `jarvis/memory/types.py` (rename `VectorSearchResult.memory_entry_id` → `entry_id`)
- Modify: `jarvis/memory/service.py` (two usages of the renamed field)
- Modify: `tests/integration/test_memory_vector_store.py` (renamed field + new tests)
- Check: `grep -rn "\.memory_entry_id" jarvis tests | grep -v __pycache__` — only `VectorSearchResult` usages change; ORM columns named `memory_entry_id` (e.g. `MemoryEvidenceRow`) stay.

**Interfaces:**
- Consumes: nothing new.
- Produces: `MemoryVectorStore(db_path: Path, dimensions: int, table_prefix: str = "memory")`; existing `upsert(entry_id: UUID, embedding: list[float])`, `search(embedding, *, limit) -> list[VectorSearchResult]` (result field now `entry_id: UUID`); new `delete_many(entry_ids: list[UUID]) -> None`. Default prefix keeps the deployed `memory_vector_ids`/`memory_vectors` tables and their `memory_entry_id` column byte-identical.

- [ ] **Step 1: Write the failing tests** (append to `tests/integration/test_memory_vector_store.py`)

```python
async def test_prefixed_store_is_isolated_from_default_store(tmp_path):
    db_path = tmp_path / "vec.db"
    memory_store = MemoryVectorStore(db_path=db_path, dimensions=3)
    document_store = MemoryVectorStore(db_path=db_path, dimensions=3, table_prefix="document")
    await memory_store.initialize()
    await document_store.initialize()

    memory_id = uuid4()
    chunk_id = uuid4()
    await memory_store.upsert(memory_id, [0.1, 0.2, 0.3])
    await document_store.upsert(chunk_id, [0.9, 0.8, 0.7])

    memory_results = await memory_store.search([0.1, 0.2, 0.3], limit=10)
    document_results = await document_store.search([0.9, 0.8, 0.7], limit=10)

    assert [r.entry_id for r in memory_results] == [memory_id]
    assert [r.entry_id for r in document_results] == [chunk_id]


async def test_delete_many_removes_entries(store):
    keep, drop = uuid4(), uuid4()
    await store.upsert(keep, [0.1, 0.2, 0.3])
    await store.upsert(drop, [0.9, 0.8, 0.7])

    await store.delete_many([drop, uuid4()])  # unknown ids are ignored

    results = await store.search([0.9, 0.8, 0.7], limit=10)
    assert [r.entry_id for r in results] == [keep]


def test_invalid_table_prefix_rejected(tmp_path):
    with pytest.raises(ValueError, match="table_prefix"):
        MemoryVectorStore(db_path=tmp_path / "x.db", dimensions=3, table_prefix="bad-prefix")
```

Also update the four existing assertions in this file from `r.memory_entry_id` / `results[0].memory_entry_id` to `.entry_id`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_memory_vector_store.py -q`
Expected: FAIL (`unexpected keyword argument 'table_prefix'`, `AttributeError: entry_id`)

- [ ] **Step 3: Implement**

In `jarvis/memory/types.py` rename the field:

```python
@dataclass(frozen=True, slots=True)
class VectorSearchResult:
    entry_id: UUID
    distance: float
    score: float
```

In `jarvis/memory/service.py` update the two `VectorSearchResult` reads:
- line ~494: `result_by_id = {result.entry_id: result for result in results}`
- (the `result.score` usages are unchanged; `mark_recalled`/dict payload lines use `RecalledMemory.memory_entry_id`, which keeps its name)

In `jarvis/memory/vector_store.py`, derive all SQL names from the prefix:

```python
_TABLE_PREFIX_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


class MemoryVectorStore:
    def __init__(self, *, db_path: Path, dimensions: int, table_prefix: str = "memory") -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if not _TABLE_PREFIX_RE.match(table_prefix):
            raise ValueError("table_prefix must be a lowercase identifier")
        self._db_path = db_path
        self._dimensions = dimensions
        self._ids_table = f"{table_prefix}_vector_ids"
        self._vectors_table = f"{table_prefix}_vectors"
        self._id_column = f"{table_prefix}_entry_id"
        self.available = False
        self.last_error: str | None = None
```

Replace every literal `memory_vector_ids` / `memory_vectors` / `memory_entry_id` in the SQL strings with f-string interpolation of `self._ids_table` / `self._vectors_table` / `self._id_column` (in `_initialize_sync`, `_existing_dimensions` — the `sqlite_master` lookup uses `self._vectors_table` as a bound parameter — `_reset_tables`, `_upsert_sync`, `_search_sync`). `_search_sync` builds results as `VectorSearchResult(entry_id=UUID(entry_id), ...)`.

Add deletion:

```python
    async def delete_many(self, entry_ids: list[UUID]) -> None:
        if not self.available or not entry_ids:
            return
        await asyncio.to_thread(self._delete_many_sync, entry_ids)

    def _delete_many_sync(self, entry_ids: list[UUID]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                for entry_id in entry_ids:
                    row = conn.execute(
                        f"SELECT rowid FROM {self._ids_table} WHERE {self._id_column} = ?",
                        (str(entry_id),),
                    ).fetchone()
                    if row is None:
                        continue
                    rowid = int(row[0])
                    conn.execute(
                        f"DELETE FROM {self._vectors_table} WHERE rowid = ?", (rowid,)
                    )
                    conn.execute(f"DELETE FROM {self._ids_table} WHERE rowid = ?", (rowid,))
```

- [ ] **Step 4: Run the memory test files**

Run: `uv run pytest tests/integration/test_memory_vector_store.py tests/integration/test_memory_service.py tests/integration/test_memory_service_summarize.py -q`
Expected: all PASS (fix any remaining `memory_entry_id` fallout — `grep -rn "VectorSearchResult(" jarvis tests` for constructors in test fakes).

- [ ] **Step 5: Commit**

```bash
git add jarvis/memory/vector_store.py jarvis/memory/types.py jarvis/memory/service.py tests/integration/test_memory_vector_store.py
git commit -m "feat: table-prefixed sqlite-vec store with delete_many"
```

---

### Task 3: Batch embedding support

**Files:**
- Modify: `jarvis/memory/embeddings.py`
- Test: `tests/unit/test_memory_embeddings.py` (append)

**Interfaces:**
- Produces: `EmbeddingProvider.embed_many(texts: list[str]) -> list[list[float]]` on the protocol and `OpenAIEmbeddingProvider` — used by Task 6.

- [ ] **Step 1: Write the failing test** (append; mirror the existing fake-client style in `tests/unit/test_memory_embeddings.py` — read it first and reuse its fake `AsyncOpenAI` double)

```python
async def test_embed_many_sends_batch_and_preserves_order():
    client = FakeClient(vectors=[[0.1, 0.2], [0.3, 0.4]])  # adapt to the file's existing fake
    provider = OpenAIEmbeddingProvider(client=client, model="embed-model", dimensions=2)

    result = await provider.embed_many(["first", "second"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert client.last_input == ["first", "second"]


async def test_embed_many_empty_input_short_circuits():
    client = FakeClient(vectors=[])
    provider = OpenAIEmbeddingProvider(client=client, model="embed-model", dimensions=2)
    assert await provider.embed_many([]) == []
    assert client.calls == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_memory_embeddings.py -q`
Expected: FAIL with `AttributeError: ... embed_many`

- [ ] **Step 3: Implement**

```python
class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    # __init__ and embed unchanged

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            input=texts,
            model=self._model,
            dimensions=self._dimensions,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]
```

Note: `PreferenceDeduplicator`'s provider usage only calls `embed`, and Python protocols are structural — existing test fakes that implement only `embed` keep type-checking at runtime (no isinstance checks exist). Do not touch them.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_memory_embeddings.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/memory/embeddings.py tests/unit/test_memory_embeddings.py
git commit -m "feat: batch embedding support (embed_many)"
```

---

### Task 4: documents + document_chunks tables (ORM + migration 0014)

**Files:**
- Modify: `jarvis/persistence/models.py` (append after `MemoryRecallEventRow`)
- Create: `alembic/versions/0014_documents.py`
- Test: `tests/integration/test_documents_migration.py`

**Interfaces:**
- Produces: `DocumentRow` (`id, source_type, source_ref, title, content_hash, status, error, created_at, updated_at, chunks`), `DocumentChunkRow` (`id, document_id, chunk_index, content, created_at, document`) — used by Tasks 5–6.

- [ ] **Step 1: Write the failing migration test**

```python
# tests/integration/test_documents_migration.py
"""Migration 0014 creates documents/document_chunks via real alembic."""

import os
import sqlite3
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["uv", "run", "alembic", "-x", f"db_url=sqlite+aiosqlite:///{db_path}", *cmd.split()],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ},
    )


def test_migration_0014_creates_document_tables(tmp_path):
    db_path = tmp_path / "test.db"
    result = _run_alembic(db_path, "upgrade head")
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    try:
        doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        chunk_cols = {row[1] for row in conn.execute("PRAGMA table_info(document_chunks)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(documents)")}
    finally:
        conn.close()

    assert doc_cols == {
        "id", "source_type", "source_ref", "title", "content_hash",
        "status", "error", "created_at", "updated_at",
    }
    assert chunk_cols == {"id", "document_id", "chunk_index", "content", "created_at"}
    assert "ix_documents_source_ref_unique" in indexes


def test_migration_0014_downgrade_drops_tables(tmp_path):
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr
    down = _run_alembic(db_path, "downgrade 0013")
    assert down.returncode == 0, down.stderr

    conn = sqlite3.connect(db_path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "documents" not in names
    assert "document_chunks" not in names
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_documents_migration.py -q`
Expected: FAIL (`PRAGMA table_info(documents)` returns no rows → assertion on empty set)

- [ ] **Step 3: Add ORM models** (append to `jarvis/persistence/models.py`, after `MemoryRecallEventRow`)

```python
class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_type: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="indexing", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    chunks: Mapped[list["DocumentChunkRow"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_documents_source_ref_unique", "source_ref", unique=True),)


class DocumentChunkRow(Base):
    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)

    document: Mapped[DocumentRow] = relationship(back_populates="chunks")

    __table_args__ = (
        Index(
            "ix_document_chunks_document_chunk_unique",
            "document_id",
            "chunk_index",
            unique=True,
        ),
    )
```

- [ ] **Step 4: Write migration 0014**

```python
# alembic/versions/0014_documents.py
"""add documents + document_chunks tables (user-content retrieval corpus)

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-09 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_created_at", "documents", ["created_at"])
    op.create_index("ix_documents_source_ref_unique", "documents", ["source_ref"], unique=True)

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index("ix_document_chunks_created_at", "document_chunks", ["created_at"])
    op.create_index(
        "ix_document_chunks_document_chunk_unique",
        "document_chunks",
        ["document_id", "chunk_index"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_document_chunk_unique", table_name="document_chunks")
    op.drop_index("ix_document_chunks_created_at", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_source_ref_unique", table_name="documents")
    op.drop_index("ix_documents_created_at", table_name="documents")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_table("documents")
```

- [ ] **Step 5: Run migration tests + full migration suite**

Run: `uv run pytest tests/integration/test_documents_migration.py tests/integration/test_migrations.py -q`
Expected: PASS (including up/down roundtrip). Also run `uv run alembic -x db_url=sqlite+aiosqlite:////tmp/docplan.db upgrade head` once as a scratch-DB sanity check.

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/models.py alembic/versions/0014_documents.py tests/integration/test_documents_migration.py
git commit -m "feat: documents + document_chunks schema (migration 0014)"
```

---

### Task 5: DocumentRepo

**Files:**
- Modify: `jarvis/persistence/repositories.py` (add `DocumentRepo` after `MemoryRecallRepo`; extend imports with `DocumentChunkRow, DocumentRow`)
- Test: `tests/integration/test_repositories_documents.py`

**Interfaces:**
- Consumes: `DocumentRow`, `DocumentChunkRow` (Task 4).
- Produces (all methods commit):
  - `get_by_source_ref(source_ref: str) -> DocumentRow | None`
  - `create(*, source_type: str, source_ref: str, title: str, content_hash: str) -> DocumentRow` (status starts `"indexing"`)
  - `mark_reingesting(document_id: UUID, *, title: str, content_hash: str) -> None` (status → `"indexing"`, clears error)
  - `set_status(document_id: UUID, status: str, *, error: str | None = None) -> None`
  - `replace_chunks(document_id: UUID, contents: list[str]) -> tuple[list[UUID], list[DocumentChunkRow]]` — returns (old chunk ids, new rows in index order)
  - `count_chunks(document_id: UUID) -> int`
  - `get_chunks_with_documents(ids: list[UUID]) -> dict[UUID, tuple[DocumentChunkRow, DocumentRow]]` — only chunks of `status == "active"` documents

- [ ] **Step 1: Write the failing tests** (schema via `Base.metadata.create_all`, mirroring `tests/integration/test_repositories_memory.py` — read its fixture setup first and copy the engine/session fixture pattern)

```python
# tests/integration/test_repositories_documents.py
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.persistence.db import Base
from jarvis.persistence.repositories import DocumentRepo


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/repo.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_create_and_get_by_source_ref(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        created = await repo.create(
            source_type="file",
            source_ref="/notes/todo.md",
            title="todo",
            content_hash="a" * 64,
        )
    async with session_factory() as session:
        found = await DocumentRepo(session).get_by_source_ref("/notes/todo.md")
    assert found is not None
    assert found.id == created.id
    assert found.status == "indexing"
    assert found.created_at.tzinfo is not None


async def test_replace_chunks_swaps_content_and_reports_old_ids(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        doc = await repo.create(
            source_type="file", source_ref="/n.md", title="n", content_hash="b" * 64
        )
        _, first = await repo.replace_chunks(doc.id, ["one", "two"])
        old_ids, second = await repo.replace_chunks(doc.id, ["three"])

    assert old_ids == [row.id for row in first]
    assert [row.content for row in second] == ["three"]
    assert [row.chunk_index for row in second] == [0]
    async with session_factory() as session:
        assert await DocumentRepo(session).count_chunks(doc.id) == 1


async def test_set_status_and_mark_reingesting(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        doc = await repo.create(
            source_type="file", source_ref="/s.md", title="s", content_hash="c" * 64
        )
        await repo.set_status(doc.id, "unindexed", error="vec down")
        await repo.mark_reingesting(doc.id, title="s2", content_hash="d" * 64)
        refreshed = await repo.get_by_source_ref("/s.md")

    assert refreshed.status == "indexing"
    assert refreshed.error is None
    assert refreshed.title == "s2"
    assert refreshed.content_hash == "d" * 64


async def test_get_chunks_with_documents_filters_inactive(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        active = await repo.create(
            source_type="file", source_ref="/a.md", title="a", content_hash="e" * 64
        )
        stale = await repo.create(
            source_type="file", source_ref="/b.md", title="b", content_hash="f" * 64
        )
        _, active_rows = await repo.replace_chunks(active.id, ["hello"])
        _, stale_rows = await repo.replace_chunks(stale.id, ["bye"])
        await repo.set_status(active.id, "active")
        await repo.set_status(stale.id, "unindexed")

        found = await repo.get_chunks_with_documents(
            [active_rows[0].id, stale_rows[0].id]
        )

    assert set(found) == {active_rows[0].id}
    chunk, document = found[active_rows[0].id]
    assert chunk.content == "hello"
    assert document.title == "a"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_repositories_documents.py -q`
Expected: FAIL with `ImportError: cannot import name 'DocumentRepo'`

- [ ] **Step 3: Implement DocumentRepo** (append to `jarvis/persistence/repositories.py`; add `DocumentChunkRow, DocumentRow` to the models import)

```python
class DocumentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_source_ref(self, source_ref: str) -> DocumentRow | None:
        stmt = select(DocumentRow).where(DocumentRow.source_ref == source_ref)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        source_type: str,
        source_ref: str,
        title: str,
        content_hash: str,
    ) -> DocumentRow:
        now = _utcnow()
        row = DocumentRow(
            source_type=source_type,
            source_ref=source_ref,
            title=title,
            content_hash=content_hash,
            status="indexing",
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        return row

    async def mark_reingesting(
        self,
        document_id: UUID,
        *,
        title: str,
        content_hash: str,
    ) -> None:
        await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id)
            .values(
                title=title,
                content_hash=content_hash,
                status="indexing",
                error=None,
                updated_at=_utcnow(),
            )
        )
        await self._session.commit()

    async def set_status(
        self,
        document_id: UUID,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id)
            .values(status=status, error=error, updated_at=_utcnow())
        )
        await self._session.commit()

    async def replace_chunks(
        self,
        document_id: UUID,
        contents: list[str],
    ) -> tuple[list[UUID], list[DocumentChunkRow]]:
        old_stmt = select(DocumentChunkRow.id).where(
            DocumentChunkRow.document_id == document_id
        )
        old_ids = list((await self._session.execute(old_stmt)).scalars())
        await self._session.execute(
            delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document_id)
        )
        now = _utcnow()
        rows = [
            DocumentChunkRow(
                document_id=document_id,
                chunk_index=index,
                content=content,
                created_at=now,
            )
            for index, content in enumerate(contents)
        ]
        self._session.add_all(rows)
        await self._session.commit()
        return old_ids, rows

    async def count_chunks(self, document_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(DocumentChunkRow)
            .where(DocumentChunkRow.document_id == document_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def get_chunks_with_documents(
        self,
        ids: list[UUID],
    ) -> dict[UUID, tuple[DocumentChunkRow, DocumentRow]]:
        if not ids:
            return {}
        stmt = (
            select(DocumentChunkRow, DocumentRow)
            .join(DocumentRow, DocumentChunkRow.document_id == DocumentRow.id)
            .where(DocumentChunkRow.id.in_(ids), DocumentRow.status == "active")
        )
        result = await self._session.execute(stmt)
        return {chunk.id: (chunk, document) for chunk, document in result.all()}
```

Check the file's existing imports: `select`, `update`, `delete`, `func` — add any that are missing.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/integration/test_repositories_documents.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_documents.py
git commit -m "feat: DocumentRepo for documents + chunks"
```

---

### Task 6: DocumentService (ingest + search) with audit events

**Files:**
- Modify: `jarvis/core/types.py` (two new `AuditEventType` members)
- Create: `jarvis/memory/documents.py`
- Test: `tests/integration/test_document_service.py`

**Interfaces:**
- Consumes: `chunk_text` (Task 1), `MemoryVectorStore` w/ prefix + `delete_many` (Task 2), `embed_many` (Task 3), `DocumentRepo` (Task 5).
- Produces:
  - `DocumentIngestOutcome(status: str, source_ref: str, document_id: UUID | None, chunk_count: int, error: str | None)` — status ∈ `created|updated|unchanged|unindexed|failed`
  - `DocumentPassage(document_id: UUID, title: str, source_ref: str, chunk_index: int, content: str, score: float)`
  - `DocumentService(session_factory, embedding_provider, vector_store, chunk_chars, chunk_overlap, max_results, min_relevance_score, audit=None)` with `ingest_path(path: Path) -> list[DocumentIngestOutcome]`, `ingest_file(path: Path) -> DocumentIngestOutcome`, `search(query: str, *, limit: int | None = None) -> list[DocumentPassage]`
  - `AuditEventType.DOCUMENT_INGESTED = "document.ingested"`, `AuditEventType.DOCUMENT_FAILED = "document.failed"`

- [ ] **Step 1: Add audit event types** (in `jarvis/core/types.py`, next to the `MEMORY_*` members)

```python
    DOCUMENT_INGESTED = "document.ingested"
    DOCUMENT_FAILED = "document.failed"
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/integration/test_document_service.py
"""End-to-end document ingestion + retrieval over real sqlite-vec."""

import math
import re
import zlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.memory.documents import DocumentService
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.db import Base
from jarvis.persistence.repositories import DocumentRepo

_DIMS = 16


class HashEmbeddings:
    """Deterministic bag-of-words embeddings: shared vocabulary → nearby vectors."""

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * _DIMS
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            vec[zlib.crc32(token.encode()) % _DIMS] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


@pytest.fixture
async def harness(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/docs.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = MemoryVectorStore(
        db_path=tmp_path / "docs.db", dimensions=_DIMS, table_prefix="document"
    )
    await store.initialize()
    service = DocumentService(
        session_factory=factory,
        embedding_provider=HashEmbeddings(),
        vector_store=store,
        chunk_chars=200,
        chunk_overlap=40,
        max_results=3,
        min_relevance_score=0.0,
    )
    yield service, factory, store, tmp_path
    await engine.dispose()


def _write_note(tmp_path, name="note.md"):
    note = tmp_path / name
    note.write_text(
        "# Home network\n\n"
        "The garage wifi network is called sparrowhawk and the password is hunter2.\n\n"
        "Grocery list: eggs, milk, coffee beans, and oat bread from the market.\n"
    )
    return note


async def test_question_answerable_only_from_document_retrieves_right_passage(harness):
    service, _, _, tmp_path = harness
    note = _write_note(tmp_path)

    outcome = await service.ingest_file(note)
    assert outcome.status == "created"
    assert outcome.chunk_count >= 2

    passages = await service.search("what is the garage wifi password?")

    assert passages
    assert "hunter2" in passages[0].content
    assert passages[0].title == "note"
    assert passages[0].source_ref == str(note.resolve())


async def test_reingest_unchanged_file_is_idempotent(harness):
    service, factory, _, tmp_path = harness
    note = _write_note(tmp_path)

    first = await service.ingest_file(note)
    second = await service.ingest_file(note)

    assert second.status == "unchanged"
    assert second.document_id == first.document_id
    async with factory() as session:
        assert await DocumentRepo(session).count_chunks(first.document_id) == first.chunk_count


async def test_changed_file_reindexes_and_drops_stale_chunks(harness):
    service, factory, store, tmp_path = harness
    note = _write_note(tmp_path)
    first = await service.ingest_file(note)

    note.write_text("Completely new content: the safe code is 4242.\n")
    second = await service.ingest_file(note)

    assert second.status == "updated"
    assert second.document_id == first.document_id
    async with factory() as session:
        assert await DocumentRepo(session).count_chunks(first.document_id) == second.chunk_count
    passages = await service.search("what is the safe code?")
    assert passages and "4242" in passages[0].content
    stale = await service.search("grocery list eggs milk")
    assert all("grocery" not in p.content.lower() for p in stale)


async def test_folder_ingest_walks_supported_files(harness):
    service, _, _, tmp_path = harness
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "a.md").write_text("alpha note about kayaks")
    (folder / "b.txt").write_text("beta note about telescopes")
    (folder / "ignored.bin").write_bytes(b"\x00\x01")

    outcomes = await service.ingest_path(folder)

    assert [o.status for o in outcomes] == ["created", "created"]


async def test_unavailable_vector_store_degrades_gracefully(harness, tmp_path):
    _, factory, _, _ = harness
    broken = MemoryVectorStore(
        db_path=tmp_path / "other.db", dimensions=_DIMS, table_prefix="document"
    )
    # never initialized → available is False
    service = DocumentService(
        session_factory=factory,
        embedding_provider=HashEmbeddings(),
        vector_store=broken,
        chunk_chars=200,
        chunk_overlap=40,
        max_results=3,
        min_relevance_score=0.0,
    )
    note = tmp_path / "degraded.md"
    note.write_text("some content that cannot be indexed right now")

    outcome = await service.ingest_file(note)
    assert outcome.status == "unindexed"

    assert await service.search("anything") == []


async def test_unreadable_file_reports_failure(harness):
    service, _, _, tmp_path = harness
    missing = tmp_path / "nope.md"

    outcome = await service.ingest_file(missing)

    assert outcome.status == "failed"
    assert outcome.error


async def test_pdf_extraction(harness):
    service, _, _, tmp_path = harness
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter  # noqa: F401  (importorskip guards)

    # Build a one-page PDF containing a known sentence.
    import io

    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        StreamObject,
    )

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    content = StreamObject()
    content.set_data(b"BT /F1 12 Tf 10 100 Td (The projector remote lives in the red drawer) Tj ET")
    ref = writer._add_object(content)
    page[NameObject("/Contents")] = ref
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            ),
            NameObject("/ProcSet"): ArrayObject(
                [NameObject("/PDF"), NameObject("/Text")]
            ),
        }
    )
    del NumberObject  # unused; keeps the import block honest if trimmed
    buf = io.BytesIO()
    writer.write(buf)
    pdf_path = tmp_path / "manual.pdf"
    pdf_path.write_bytes(buf.getvalue())

    outcome = await service.ingest_file(pdf_path)
    assert outcome.status == "created"

    passages = await service.search("where is the projector remote?")
    assert passages and "red drawer" in passages[0].content
```

(If hand-building the PDF content stream proves brittle with the installed pypdf version, simplify the test to monkeypatching `jarvis.memory.documents._extract_pdf_text` to return the sentence — the goal is exercising the `.pdf` routing, not pypdf itself. Keep at least the routing assertion.)

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/integration/test_document_service.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis.memory.documents'`

- [ ] **Step 4: Add pypdf dependency**

In `pyproject.toml` `dependencies`, add `"pypdf>=5.0"` after `"openai>=1.68",`. Then run `uv sync` (or `uv lock && uv sync`) so the lockfile picks it up.

- [ ] **Step 5: Implement the service**

```python
# jarvis/memory/documents.py
"""Document corpus ingestion + retrieval.

Chunks user content (markdown, text, PDF), embeds chunks into the prefixed
sqlite-vec tables, and serves passage search for the `search_documents`
agent tool. Idempotency: a file whose sha256 matches its stored
content_hash (and is fully indexed) is skipped, mirroring the
recall-summary source-hash dedup.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.memory.chunking import chunk_text
from jarvis.memory.embeddings import EmbeddingProvider
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.repositories import DocumentRepo

_log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".pdf"})


@dataclass(frozen=True, slots=True)
class DocumentIngestOutcome:
    status: str  # created | updated | unchanged | unindexed | failed
    source_ref: str
    document_id: UUID | None = None
    chunk_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentPassage:
    document_id: UUID
    title: str
    source_ref: str
    chunk_index: int
    content: str
    score: float


class DocumentService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        vector_store: MemoryVectorStore,
        chunk_chars: int,
        chunk_overlap: int,
        max_results: int,
        min_relevance_score: float,
        audit=None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._chunk_chars = chunk_chars
        self._chunk_overlap = chunk_overlap
        self._max_results = max_results
        self._min_relevance_score = min_relevance_score
        self._audit = audit

    @property
    def search_available(self) -> bool:
        return self._vector_store.available

    async def ingest_path(self, path: Path) -> list[DocumentIngestOutcome]:
        path = path.expanduser()
        if path.is_dir():
            files = sorted(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_SUFFIXES
            )
        else:
            files = [path]
        return [await self.ingest_file(file) for file in files]

    async def ingest_file(self, path: Path) -> DocumentIngestOutcome:
        path = path.expanduser()
        source_ref = str(path.resolve())
        try:
            raw = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            return await self._failed(source_ref, document_id=None, error=str(exc))

        content_hash = hashlib.sha256(raw).hexdigest()
        async with self._session_factory() as session:
            existing = await DocumentRepo(session).get_by_source_ref(source_ref)
        if (
            existing is not None
            and existing.content_hash == content_hash
            and existing.status == "active"
        ):
            async with self._session_factory() as session:
                chunk_count = await DocumentRepo(session).count_chunks(existing.id)
            return DocumentIngestOutcome(
                status="unchanged",
                source_ref=source_ref,
                document_id=existing.id,
                chunk_count=chunk_count,
            )

        try:
            text = await asyncio.to_thread(_extract_text, path, raw)
            chunks = chunk_text(
                text, max_chars=self._chunk_chars, overlap=self._chunk_overlap
            )
        except Exception as exc:
            return await self._failed(
                source_ref, document_id=existing.id if existing else None, error=str(exc)
            )
        if not chunks:
            return await self._failed(
                source_ref,
                document_id=existing.id if existing else None,
                error="no extractable text",
            )

        async with self._session_factory() as session:
            repo = DocumentRepo(session)
            if existing is None:
                document = await repo.create(
                    source_type="file",
                    source_ref=source_ref,
                    title=path.stem,
                    content_hash=content_hash,
                )
                document_id = document.id
            else:
                document_id = existing.id
                await repo.mark_reingesting(
                    document_id, title=path.stem, content_hash=content_hash
                )
            old_chunk_ids, new_rows = await repo.replace_chunks(document_id, chunks)

        status = "created" if existing is None else "updated"
        await self._vector_store.delete_many(old_chunk_ids)

        if not self._vector_store.available:
            return await self._unindexed(
                source_ref,
                document_id=document_id,
                chunk_count=len(new_rows),
                error=self._vector_store.last_error or "vector store unavailable",
            )

        try:
            embeddings = await self._embedding_provider.embed_many(
                [row.content for row in new_rows]
            )
            for row, embedding in zip(new_rows, embeddings, strict=True):
                await self._vector_store.upsert(row.id, embedding)
        except Exception as exc:
            return await self._unindexed(
                source_ref,
                document_id=document_id,
                chunk_count=len(new_rows),
                error=str(exc),
            )

        async with self._session_factory() as session:
            await DocumentRepo(session).set_status(document_id, "active")
        await self._emit(
            AuditEventType.DOCUMENT_INGESTED,
            payload={
                "document_id": str(document_id),
                "source_ref": source_ref,
                "status": status,
                "chunks": len(new_rows),
            },
        )
        return DocumentIngestOutcome(
            status=status,
            source_ref=source_ref,
            document_id=document_id,
            chunk_count=len(new_rows),
        )

    async def search(self, query: str, *, limit: int | None = None) -> list[DocumentPassage]:
        effective_limit = self._max_results if limit is None else limit
        if not query.strip() or effective_limit <= 0 or not self._vector_store.available:
            return []
        try:
            embedding = await self._embedding_provider.embed(query)
            results = await self._vector_store.search(embedding, limit=effective_limit)
        except Exception as exc:
            _log.warning("document search failed: %s", exc)
            return []
        filtered = [r for r in results if r.score >= self._min_relevance_score]
        if not filtered:
            return []
        async with self._session_factory() as session:
            found = await DocumentRepo(session).get_chunks_with_documents(
                [result.entry_id for result in filtered]
            )
        passages: list[DocumentPassage] = []
        for result in filtered:
            match = found.get(result.entry_id)
            if match is None:
                continue
            chunk, document = match
            passages.append(
                DocumentPassage(
                    document_id=document.id,
                    title=document.title,
                    source_ref=document.source_ref,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    score=result.score,
                )
            )
        return passages

    async def _failed(
        self, source_ref: str, *, document_id: UUID | None, error: str
    ) -> DocumentIngestOutcome:
        if document_id is not None:
            await self._set_status_best_effort(document_id, "error", error=error)
        await self._emit(
            AuditEventType.DOCUMENT_FAILED,
            payload={"source_ref": source_ref, "stage": "extract", "error": error},
        )
        return DocumentIngestOutcome(
            status="failed", source_ref=source_ref, document_id=document_id, error=error
        )

    async def _unindexed(
        self, source_ref: str, *, document_id: UUID, chunk_count: int, error: str
    ) -> DocumentIngestOutcome:
        await self._set_status_best_effort(document_id, "unindexed", error=error)
        await self._emit(
            AuditEventType.DOCUMENT_FAILED,
            payload={"source_ref": source_ref, "stage": "index", "error": error},
        )
        return DocumentIngestOutcome(
            status="unindexed",
            source_ref=source_ref,
            document_id=document_id,
            chunk_count=chunk_count,
            error=error,
        )

    async def _set_status_best_effort(
        self, document_id: UUID, status: str, *, error: str | None
    ) -> None:
        try:
            async with self._session_factory() as session:
                await DocumentRepo(session).set_status(document_id, status, error=error)
        except Exception:
            _log.exception("failed to update document status")

    async def _emit(self, event_type: AuditEventType, *, payload: dict) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.emit(AuditEvent(type=event_type, payload=payload))
        except Exception:
            return


def _extract_text(path: Path, raw: bytes) -> str:
    if path.suffix.lower() == ".pdf":
        return _extract_pdf_text(raw)
    return raw.decode("utf-8", errors="replace")


def _extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(part.strip() for part in pages if part.strip())
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/integration/test_document_service.py -q`
Expected: PASS. If the hand-built PDF test fails on text extraction, apply the fallback noted in Step 2 (monkeypatch `_extract_pdf_text`).

- [ ] **Step 7: Commit**

```bash
git add jarvis/memory/documents.py jarvis/core/types.py pyproject.toml uv.lock tests/integration/test_document_service.py
git commit -m "feat: DocumentService — idempotent ingest + passage search"
```

---

### Task 7: search_documents agent tool + build_agent tools plumbing

**Files:**
- Create: `jarvis/agents/document_tool.py`
- Modify: `jarvis/agents/factory.py` (`build_agent(..., tools=None)`)
- Modify: `jarvis/agents/runner.py` (`AgentRunner(..., tools=None)` → pass through)
- Modify: `jarvis/actions/service.py` (`ActionService(..., tools=None)` → pass through to its `build_agent`)
- Modify: `jarvis/scheduler/scheduler.py` (`Scheduler(..., tools=None)` → forward to its internal `AgentRunner`)
- Test: `tests/integration/test_document_tool.py`

**Interfaces:**
- Consumes: `DocumentService.search(query) -> list[DocumentPassage]` (Task 6).
- Produces: `build_document_search_tool(document_service) -> FunctionTool` (SDK tool named `search_documents`); `build_agent(*, llm_config, mcp_servers_provider, trigger=None, explicit_model=None, model_provider=None, model_override=None, tools=None)`; `AgentRunner`/`ActionService`/`Scheduler` accept `tools: list | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_document_tool.py
"""search_documents function tool formatting + agent wiring."""

import json
from uuid import uuid4

from jarvis.agents.document_tool import build_document_search_tool
from jarvis.agents.factory import build_agent
from jarvis.config.schema import LLMConfig
from jarvis.memory.documents import DocumentPassage


class FakeDocumentService:
    def __init__(self, passages):
        self._passages = passages
        self.queries = []

    async def search(self, query, *, limit=None):
        self.queries.append(query)
        return self._passages


def _passage(content, title="note", chunk_index=0, score=0.9):
    return DocumentPassage(
        document_id=uuid4(),
        title=title,
        source_ref=f"/docs/{title}.md",
        chunk_index=chunk_index,
        content=content,
        score=score,
    )


async def _invoke(tool, query):
    return await tool.on_invoke_tool(None, json.dumps({"query": query}))


async def test_tool_returns_formatted_passages():
    service = FakeDocumentService([_passage("the wifi password is hunter2")])
    tool = build_document_search_tool(service)

    assert tool.name == "search_documents"
    output = await _invoke(tool, "wifi password")

    assert service.queries == ["wifi password"]
    assert "hunter2" in output
    assert "note" in output


async def test_tool_reports_no_matches():
    tool = build_document_search_tool(FakeDocumentService([]))
    output = await _invoke(tool, "anything")
    assert "No matching passages" in output


async def test_tool_output_is_bounded():
    passages = [_passage("x" * 5_000, title=f"n{i}", chunk_index=i) for i in range(10)]
    tool = build_document_search_tool(FakeDocumentService(passages))
    output = await _invoke(tool, "big")
    assert len(output) <= 7_000


def test_build_agent_attaches_tools():
    llm = LLMConfig(base_url="http://localhost", api_key="k", model="m")
    tool = build_document_search_tool(FakeDocumentService([]))
    agent, _ = build_agent(
        llm_config=llm, mcp_servers_provider=list, tools=[tool]
    )
    assert [t.name for t in agent.tools] == ["search_documents"]


def test_build_agent_defaults_to_no_tools():
    llm = LLMConfig(base_url="http://localhost", api_key="k", model="m")
    agent, _ = build_agent(llm_config=llm, mcp_servers_provider=list)
    assert agent.tools == []
```

(If `on_invoke_tool`'s first argument requires a real `RunContextWrapper`, adapt `_invoke` — check `agents.tool.FunctionTool` in the installed SDK; a `SimpleNamespace()` or `RunContextWrapper(context=None)` is acceptable.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_document_tool.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis.agents.document_tool'`

- [ ] **Step 3: Implement the tool**

```python
# jarvis/agents/document_tool.py
"""Native SDK tool exposing document passage search to agent runs."""

from __future__ import annotations

from agents import function_tool

_MAX_PASSAGE_CHARS = 1_200
# Total tool output cap: passages are additional turn context, so keep the
# whole result well under the runner's history budget.
_MAX_TOTAL_CHARS = 6_000


def build_document_search_tool(document_service):
    @function_tool(name_override="search_documents")
    async def search_documents(query: str) -> str:
        """Search the user's own documents (notes, PDFs, attachments) for passages
        relevant to the query. Use this whenever the question may be answerable
        from the user's personal content rather than general knowledge."""
        passages = await document_service.search(query)
        if not passages:
            return "No matching passages found in the document index."
        blocks: list[str] = []
        total = 0
        for passage in passages:
            snippet = passage.content[:_MAX_PASSAGE_CHARS]
            block = (
                f"[{passage.title} · {passage.source_ref} · chunk {passage.chunk_index} "
                f"· score {passage.score:.2f}]\n{snippet}"
            )
            if total + len(block) > _MAX_TOTAL_CHARS:
                break
            blocks.append(block)
            total += len(block)
        return "\n\n".join(blocks)

    return search_documents
```

- [ ] **Step 4: Thread `tools` through factory, runner, action service, scheduler**

`jarvis/agents/factory.py`:

```python
def build_agent(
    *,
    llm_config: LLMConfig,
    mcp_servers_provider: Callable[[], list],
    trigger=None,
    explicit_model: Any = None,
    model_provider: Callable[[], str] | None = None,
    model_override: str | None = None,
    tools: list | None = None,
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
        tools=list(tools or []),
    )
    return agent, str(model)
```

`jarvis/agents/runner.py` — add constructor param `tools: Any = None` (store as `self._tools = tools`), and in `run()` pass `tools=self._tools` to `build_agent(...)`.

`jarvis/actions/service.py` — add constructor param `tools: Any = None` (inspect `__init__` for where other deps are stored) and pass `tools=self._tools` in its `build_agent(...)` call (~line 79) so resumed runs rehydrate with the same toolset.

`jarvis/scheduler/scheduler.py` — add constructor param `tools: Any = None` and forward `tools=tools` to the `AgentRunner(...)` it builds (~line 98).

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/integration/test_document_tool.py tests/integration/test_agent_runner.py tests/integration/test_agent_runner_memory.py tests/integration/test_action_service.py tests/integration/test_scheduler.py -q`
Expected: PASS (new params are optional; existing tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add jarvis/agents/document_tool.py jarvis/agents/factory.py jarvis/agents/runner.py jarvis/actions/service.py jarvis/scheduler/scheduler.py tests/integration/test_document_tool.py
git commit -m "feat: search_documents agent tool threaded through all agent builders"
```

---

### Task 8: Config surface for documents

**Files:**
- Modify: `jarvis/config/schema.py` (`MemoryConfig`)
- Modify: `config/jarvis.yaml.example`
- Test: `tests/unit/test_config_schema.py` (append)

**Interfaces:**
- Produces: `MemoryConfig.documents_folder: str | None = None`, `document_chunk_chars: int = 1800 (ge=200)`, `document_chunk_overlap: int = 200 (ge=0)`, `max_document_results: int = 5 (ge=0, le=20)`, validator `document_chunk_overlap < document_chunk_chars`. Used by Task 9.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_config_schema.py`, matching its existing style)

```python
def test_memory_document_defaults():
    cfg = MemoryConfig()
    assert cfg.documents_folder is None
    assert cfg.document_chunk_chars == 1800
    assert cfg.document_chunk_overlap == 200
    assert cfg.max_document_results == 5


def test_document_overlap_must_be_smaller_than_chunk():
    with pytest.raises(ValidationError):
        MemoryConfig(document_chunk_chars=300, document_chunk_overlap=300)
```

(Import `MemoryConfig` / `ValidationError` if the file doesn't already.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_config_schema.py -q`
Expected: FAIL with `AttributeError: documents_folder` (extra=forbid also rejects unknown kwargs)

- [ ] **Step 3: Implement**

In `MemoryConfig` add after `preference_dedup_max_judge_calls`:

```python
    # Document corpus (notes, PDFs) ingestion + retrieval.
    documents_folder: str | None = None
    document_chunk_chars: int = Field(default=1800, ge=200)
    document_chunk_overlap: int = Field(default=200, ge=0)
    max_document_results: int = Field(default=5, ge=0, le=20)
```

Extend the existing `_thresholds_ordered` validator (or add a second `model_validator`):

```python
    @model_validator(mode="after")
    def _document_chunking_sane(self) -> "MemoryConfig":
        if self.document_chunk_overlap >= self.document_chunk_chars:
            raise ValueError(
                "document_chunk_overlap must be strictly less than document_chunk_chars"
            )
        return self
```

In `config/jarvis.yaml.example`, after `min_relevance_score: 0.25` add:

```yaml
  # Folder of user documents (.md/.txt/.pdf) for `jarvis ingest` and the
  # search_documents agent tool. Leave empty to pass paths explicitly.
  documents_folder:
  document_chunk_chars: 1800
  document_chunk_overlap: 200
  max_document_results: 5
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_config_schema.py tests/unit/test_config_loader.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/config/schema.py config/jarvis.yaml.example tests/unit/test_config_schema.py
git commit -m "feat: document ingestion config knobs"
```

---

### Task 9: Bootstrap wiring + `jarvis ingest` CLI

**Files:**
- Modify: `jarvis/main.py` (build `DocumentService`, attach tool, add to `AppContext`)
- Modify: `jarvis/cli.py` (new `ingest` command)
- Test: `tests/integration/test_cli.py` (append) and `tests/integration/test_main_smoke.py` (verify still passes; extend if it asserts `AppContext` fields)

**Interfaces:**
- Consumes: `DocumentService` (Task 6), `build_document_search_tool` (Task 7), config knobs (Task 8).
- Produces: `AppContext.document_service: DocumentService | None`; `python -m jarvis ingest [PATH]`.

- [ ] **Step 1: Write the failing CLI test** (append to `tests/integration/test_cli.py` — read the file first and mirror how existing tests build config dirs/db URLs and invoke the Typer app; reuse its fixtures)

```python
def test_ingest_command_indexes_folder_and_is_idempotent(tmp_path, cli_env):
    # cli_env: reuse/adapt the existing fixture that yields (config_dir, db_url)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("the boiler pilot light reset code is 7-7-1")

    result = runner.invoke(app, ["ingest", str(docs), *cli_env_args])
    assert result.exit_code == 0, result.output
    assert "created" in result.output

    again = runner.invoke(app, ["ingest", str(docs), *cli_env_args])
    assert again.exit_code == 0, again.output
    assert "unchanged" in again.output
```

Adapt fixture names to what `test_cli.py` actually provides (it exists — follow its `invoke`/`check-config` test pattern, including any LLM stubbing it does). If the existing CLI tests stub the LLM client, reuse that stub; embeddings must not hit the network — if unavoidable, monkeypatch `OpenAIEmbeddingProvider.embed_many`/`embed` to a deterministic fake for this test.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_cli.py -q`
Expected: new test FAILS (`No such command 'ingest'`)

- [ ] **Step 3: Wire DocumentService in `jarvis/main.py`**

- Import `DocumentService` from `jarvis.memory.documents` and `build_document_search_tool` from `jarvis.agents.document_tool`.
- Add `document_service: DocumentService | None` field to `AppContext` (after `memory_service`).
- Refactor `_build_memory_service` minimally: extract the shared pieces and add a sibling builder, called right after `memory_service` is built in `bootstrap`:

```python
async def _build_document_service(
    *,
    cfg: LoadedConfig,
    db_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    llm_client: AsyncOpenAI,
    audit: AuditLogger,
) -> DocumentService | None:
    if not cfg.jarvis.memory.enabled:
        return None
    db_path = _local_sqlite_db_path(db_url)
    if db_path is None:
        return None

    vector_store = MemoryVectorStore(
        db_path=db_path,
        dimensions=cfg.jarvis.memory.embedding_dimensions,
        table_prefix="document",
    )
    await vector_store.initialize()

    embedding_model = cfg.jarvis.memory.embedding_model or cfg.jarvis.llm.model
    embedding_provider = OpenAIEmbeddingProvider(
        client=llm_client,
        model=embedding_model,
        dimensions=cfg.jarvis.memory.embedding_dimensions,
    )
    return DocumentService(
        session_factory=session_factory,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
        chunk_chars=cfg.jarvis.memory.document_chunk_chars,
        chunk_overlap=cfg.jarvis.memory.document_chunk_overlap,
        max_results=cfg.jarvis.memory.max_document_results,
        min_relevance_score=cfg.jarvis.memory.min_relevance_score,
        audit=audit,
    )
```

In `bootstrap`, after `memory_service = ...`:

```python
    document_service = await _build_document_service(
        cfg=cfg,
        db_url=db_url,
        session_factory=factory,
        llm_client=llm_client,
        audit=audit,
    )
    agent_tools = (
        [build_document_search_tool(document_service)] if document_service is not None else []
    )
```

Pass `tools=agent_tools` to `AgentRunner(...)`, `ActionService(...)`, and `Scheduler(...)`; add `document_service=document_service` to the `AppContext(...)` construction.

- [ ] **Step 4: Add the CLI command** (in `jarvis/cli.py`)

```python
@app.command("ingest")
def ingest_command(
    path: Path = typer.Argument(
        None, help="File or folder to ingest (defaults to memory.documents_folder)"
    ),
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
    db_url: str = typer.Option(_DEFAULT_DB, "--db-url", help="SQLAlchemy DB URL"),
) -> None:
    """Index documents (.md/.txt/.pdf) so the agent can answer from them."""
    asyncio.run(_ingest_async(path, config_dir, db_url))


async def _ingest_async(path: Path | None, config_dir: Path, db_url: str) -> None:
    ctx = await bootstrap(config_dir=config_dir, db_url=db_url)
    try:
        if ctx.document_service is None:
            typer.echo("document ingestion unavailable (memory disabled or non-local DB)")
            raise typer.Exit(code=1)
        folder = ctx.config.jarvis.memory.documents_folder
        target = path or (Path(folder) if folder else None)
        if target is None:
            typer.echo("no path given and memory.documents_folder is not configured")
            raise typer.Exit(code=2)
        outcomes = await ctx.document_service.ingest_path(target)
        if not outcomes:
            typer.echo("no supported files found (.md, .markdown, .txt, .pdf)")
        for outcome in outcomes:
            detail = f" ({outcome.error})" if outcome.error else ""
            typer.echo(
                f"{outcome.status:10s} {outcome.source_ref} "
                f"[{outcome.chunk_count} chunks]{detail}"
            )
        failures = [o for o in outcomes if o.status in ("failed", "unindexed")]
        if failures:
            raise typer.Exit(code=3)
    finally:
        await ctx.shutdown()
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/integration/test_cli.py tests/integration/test_main_smoke.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add jarvis/main.py jarvis/cli.py tests/integration/test_cli.py
git commit -m "feat: wire DocumentService into bootstrap + jarvis ingest CLI"
```

---

### Task 10: Full verification + docs

**Files:**
- Modify: `CLAUDE.md` (one line in Layout for `jarvis/memory/` mentioning documents; one row in Commands for `jarvis ingest` if the table lists CLI verbs)

- [ ] **Step 1: Lint + autofix**

Run: `make fmt && uv run ruff check jarvis tests`
Expected: clean

- [ ] **Step 2: Full test suite**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: all pass, no errors

- [ ] **Step 3: Manual smoke of the acceptance criteria** (uses the real config only if present; otherwise rely on the integration tests, which already cover all four "done when" bullets)

```bash
# optional, needs a working config/ + LLM endpoint:
echo "The attic ladder release lever is behind the left joist." > /tmp/attic.md
uv run python -m jarvis ingest /tmp/attic.md
uv run python -m jarvis invoke "where is the attic ladder release lever?"
```

- [ ] **Step 4: Update CLAUDE.md**

- Layout bullet: change the `jarvis/memory/` line to `preferences + vector recall (sqlite-vec), semantic dedup, document corpus ingest/search (documents.py, chunking.py)`.
- Commands table: add `| Ingest documents | uv run python -m jarvis ingest <path> |` after the invoke row.

- [ ] **Step 5: Commit + push branch + PR**

```bash
git add CLAUDE.md
git commit -m "docs: document ingestion in CLAUDE.md"
git push -u origin feat/document-ingestion-retrieval
gh pr create --title "feat: document corpus ingestion + search_documents agent tool" --body "..."
```

---

## Self-Review Notes

- **Spec coverage:** ingestion pipeline (Tasks 1, 4–6), retrieval tool during a run (Task 7), incremental re-index via source hash (Task 6 + tests), migration w/ real-alembic test (Task 4), graceful degradation (Tasks 2/6 + tests), token budget (tool output cap, Task 7), repository-only persistence (Task 5), config + CLI entry (Tasks 8–9), `make check` (Task 10). Fastmail/Drive connectors are explicitly out of scope for this plan — folder corpus first (the goal lists them as alternatives).
- **Type consistency:** `VectorSearchResult.entry_id` (Task 2) is what Task 6 reads; `DocumentRepo.replace_chunks -> tuple[list[UUID], list[DocumentChunkRow]]` matches Task 6's unpacking; `build_document_search_tool(document_service)` matches Task 9's wiring; `MemoryConfig.document_*` names match Task 9's reads.
- **Known judgment calls:** documents index in the same SQLite file under `document_*` vec tables (consistent with memory); document status vocabulary `indexing|active|unindexed|error` mirrors memory entries plus an explicit `error`; the PDF test may fall back to monkeypatching extraction if hand-built PDFs are brittle.

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
        "id",
        "source_type",
        "source_ref",
        "title",
        "content_hash",
        "status",
        "error",
        "created_at",
        "updated_at",
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
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "documents" not in names
    assert "document_chunks" not in names

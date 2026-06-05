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

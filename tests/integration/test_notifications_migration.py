"""Migration 0013 — notifications table, via real alembic on a scratch DB."""

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


def test_notifications_table_created_with_expected_columns(tmp_path):
    db_path = tmp_path / "test.db"
    result = _run_alembic(db_path, "upgrade head")
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}
        assert columns == {
            "id",
            "priority",
            "source",
            "text",
            "status",
            "created_at",
            "digested_at",
        }
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(notifications)")}
        assert "ix_notifications_status_created_at" in indexes
    finally:
        conn.close()


def test_notifications_downgrade_drops_table(tmp_path):
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr
    down = _run_alembic(db_path, "downgrade 0012")
    assert down.returncode == 0, down.stderr

    conn = sqlite3.connect(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notifications'"
            )
        )
        assert rows == []
    finally:
        conn.close()

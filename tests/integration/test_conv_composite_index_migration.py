"""The composite index must exist after `alembic upgrade head` runs."""

import os
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "-x",
            f"db_url=sqlite+aiosqlite:///{db_path}",
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
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='conversations'"
        ).fetchall()
    finally:
        c.close()

    names = {r[0] for r in rows}
    assert "ix_conversations_lookup" in names, (
        f"composite index not found. indexes on conversations: {names}"
    )

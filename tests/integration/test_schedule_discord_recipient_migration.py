"""Alembic migration 0005: schedules.discord_user_id column."""

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


def _cols(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info('schedules')")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def test_upgrade_adds_discord_user_id_column(tmp_path):
    db_path = tmp_path / "t.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr
    assert "discord_user_id" in _cols(db_path)


def test_downgrade_removes_discord_user_id_column(tmp_path):
    db_path = tmp_path / "t.db"
    assert _run_alembic(db_path, "upgrade head").returncode == 0
    down = _run_alembic(db_path, "downgrade -1")
    assert down.returncode == 0, down.stderr
    assert "discord_user_id" not in _cols(db_path)

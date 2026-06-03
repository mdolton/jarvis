"""Alembic migration 0003: oauth_credentials + oauth_pending tables exist after upgrade."""

import os
import sqlite3
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [
            "uv", "run", "alembic",
            "-x", f"db_url=sqlite+aiosqlite:///{db_path}",
            *cmd.split(),
        ],
        capture_output=True, text=True, cwd=cwd, env={**os.environ},
    )


def test_migrate_up_creates_oauth_tables(tmp_path):
    db_path = tmp_path / "test.db"
    # Pin to 0003 (the migration under test) rather than head, so later
    # migrations don't change what `downgrade -1` reverts.
    result = _run_alembic(db_path, "upgrade 0003")
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert "oauth_credentials" in tables, f"missing table; got {tables}"
    assert "oauth_pending" in tables, f"missing table; got {tables}"

    cur.execute("PRAGMA table_info('oauth_credentials')")
    cols = {row[1] for row in cur.fetchall()}
    expected = {
        "provider_key", "client_id_enc", "client_secret_enc",
        "access_token_enc", "refresh_token_enc", "token_expires_at",
        "scopes_granted", "status", "last_error", "connected_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing cols; got {cols}"
    conn.close()


def test_migrate_down_drops_oauth_tables(tmp_path):
    db_path = tmp_path / "test.db"
    # Pin to 0003 so `downgrade -1` reverts the oauth migration (0003 -> 0002),
    # regardless of any later migrations on top of head.
    up = _run_alembic(db_path, "upgrade 0003")
    assert up.returncode == 0, up.stderr
    down = _run_alembic(db_path, "downgrade -1")
    assert down.returncode == 0, down.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert "oauth_credentials" not in tables
    assert "oauth_pending" not in tables
    conn.close()

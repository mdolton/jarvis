"""0015 creates the auth tables (users, auth_codes, sessions, webauthn_credentials,
recovery_codes) — schema only, no seed data."""

import os
import sqlite3
import subprocess
from pathlib import Path

AUTH_TABLES = {"users", "auth_codes", "sessions", "webauthn_credentials", "recovery_codes"}


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["uv", "run", "alembic", "-x", f"db_url=sqlite+aiosqlite:///{db_path}", *cmd.split()],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ},
    )


def test_0015_creates_auth_tables_and_indexes(tmp_path):
    db_path = tmp_path / "test.db"
    r = _run_alembic(db_path, "upgrade 0015")
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert AUTH_TABLES <= tables

    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {row[0] for row in cur.fetchall()}
    assert {
        "ix_auth_codes_user_id",
        "ix_sessions_user_id",
        "ix_sessions_token_hash_unique",
        "ix_webauthn_credentials_user_id",
        "ix_recovery_codes_user_id",
    } <= indexes

    # token_hash and email must be UNIQUE.
    cur.execute("PRAGMA index_list(sessions)")
    unique_by_name = {row[1]: bool(row[2]) for row in cur.fetchall()}
    assert unique_by_name["ix_sessions_token_hash_unique"] is True
    cur.execute("PRAGMA index_list(users)")
    assert any(bool(row[2]) for row in cur.fetchall()), "users.email UNIQUE constraint missing"

    # Every child table cascades from users.
    for table in AUTH_TABLES - {"users"}:
        cur.execute(f"PRAGMA foreign_key_list({table})")
        fks = cur.fetchall()
        assert len(fks) == 1, f"{table} should have exactly one FK"
        assert fks[0][2] == "users" and fks[0][6] == "CASCADE", f"{table} FK is not users CASCADE"

    conn.close()


def test_0015_downgrade_removes_auth_tables(tmp_path):
    db_path = tmp_path / "test.db"
    assert _run_alembic(db_path, "upgrade 0015").returncode == 0
    r = _run_alembic(db_path, "downgrade 0014")
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    conn.close()
    assert not (AUTH_TABLES & tables)

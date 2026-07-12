"""0016 creates webauthn_challenges — server-side single-use ceremony challenges."""

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


def test_0016_creates_webauthn_challenges(tmp_path):
    db_path = tmp_path / "test.db"
    r = _run_alembic(db_path, "upgrade 0016")
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    assert "webauthn_challenges" in {row[0] for row in cur.fetchall()}

    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    assert "ix_webauthn_challenges_user_id" in {row[0] for row in cur.fetchall()}

    # user_id is NULLABLE (login challenges have no user yet) and cascades.
    cur.execute("PRAGMA table_info(webauthn_challenges)")
    nullable_by_name = {row[1]: not row[3] for row in cur.fetchall()}
    assert nullable_by_name["user_id"] is True
    assert nullable_by_name["challenge"] is False
    assert nullable_by_name["expires_at"] is False

    cur.execute("PRAGMA foreign_key_list(webauthn_challenges)")
    fks = cur.fetchall()
    assert len(fks) == 1
    assert fks[0][2] == "users" and fks[0][6] == "CASCADE"
    conn.close()


def test_0016_downgrade_removes_table(tmp_path):
    db_path = tmp_path / "test.db"
    assert _run_alembic(db_path, "upgrade 0016").returncode == 0
    r = _run_alembic(db_path, "downgrade 0015")
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    conn.close()
    assert "webauthn_challenges" not in tables

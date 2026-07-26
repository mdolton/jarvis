"""0017 adds schedules.mcp_servers without disturbing existing rows."""

import os
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

from jarvis.oauth.crypto import generate_key


def _run_alembic(db_path: Path, cmd: str, env=None) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["uv", "run", "alembic", "-x", f"db_url=sqlite+aiosqlite:///{db_path}", *cmd.split()],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env or {**os.environ},
    )


def _env():
    return {**os.environ, "JARVIS_SECRETS_KEY": generate_key()}


def test_0017_adds_nullable_mcp_servers_column(tmp_path):
    db_path = tmp_path / "test.db"
    r = _run_alembic(db_path, "upgrade 0017", env=_env())
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_path)
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(schedules)")}
    assert "mcp_servers" in cols
    assert cols["mcp_servers"][3] == 0  # notnull == 0, so old rows need no backfill
    conn.close()


def test_0017_preserves_existing_schedules_and_defaults_to_all_servers(tmp_path):
    """An existing schedule must survive with NULL scope — which the app reads
    as "every connected server", i.e. exactly its pre-migration behavior."""
    db_path = tmp_path / "test.db"
    env = _env()
    r = _run_alembic(db_path, "upgrade 0016", env=env)
    assert r.returncode == 0, r.stderr

    schedule_id = uuid4().hex  # hex, never str(uuid4()) — SQLAlchemy Uuid on SQLite
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO schedules (id, name, description, cron_expr, timezone, prompt, "
        "output_mode, notify_on_error, enabled, created_at, updated_at) "
        "VALUES (?, 'Daily Brief', '', '0 8 * * *', 'America/Los_Angeles', 'brief me', "
        "'discord', 1, 1, '2026-07-26 08:00:00', '2026-07-26 08:00:00')",
        (schedule_id,),
    )
    conn.commit()
    conn.close()

    r = _run_alembic(db_path, "upgrade 0017", env=env)
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_path)
    rows = list(conn.execute("SELECT name, mcp_servers FROM schedules"))
    conn.close()
    assert rows == [("Daily Brief", None)]


def test_0017_downgrade_drops_the_column(tmp_path):
    db_path = tmp_path / "test.db"
    env = _env()
    assert _run_alembic(db_path, "upgrade 0017", env=env).returncode == 0

    r = _run_alembic(db_path, "downgrade 0016", env=env)
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(schedules)")}
    conn.close()
    assert "mcp_servers" not in cols

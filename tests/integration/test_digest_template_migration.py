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


def test_digest_templates_migration_roundtrip(tmp_path):
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('digest_templates')").fetchall()
        }
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list('digest_templates')").fetchall()
        }
    assert {
        "id",
        "key",
        "name",
        "description",
        "category",
        "prompt",
        "default_cron_expr",
        "default_timezone",
        "default_output_mode",
        "default_model",
        "default_discord_user_id",
        "built_in",
        "enabled",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert "ix_digest_templates_enabled_category_name" in indexes
    assert "ix_digest_templates_enabled" not in indexes

    check = _run_alembic(db_path, "check")
    assert check.returncode == 0, check.stderr

    down = _run_alembic(db_path, "downgrade 0006")
    assert down.returncode == 0, down.stderr

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "digest_templates" not in tables

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


def _table_exists(db_path: Path, table_name: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    return row is not None


def _column_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _index_names(db_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
    return {row[1] for row in rows}


def _assert_actions_schema(db_path: Path) -> None:
    assert _table_exists(db_path, "actions")
    assert _column_names(db_path, "actions") == {
        "id",
        "status",
        "decision",
        "conversation_id",
        "trigger_id",
        "channel_kind",
        "channel_ref",
        "server_name",
        "tool_name",
        "tool_call_id",
        "arguments_json",
        "run_state_json",
        "approval_item_json",
        "model",
        "created_at",
        "decided_at",
        "completed_at",
        "decision_reason",
        "error",
    }
    assert {
        "ix_actions_status",
        "ix_actions_created_at",
        "ix_actions_status_created_at",
        "ix_actions_conversation_id",
        "ix_actions_trigger_id",
    }.issubset(_index_names(db_path, "actions"))


def test_action_inbox_migration_roundtrip(tmp_path):
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade 0006")
    assert up.returncode == 0, up.stderr
    _assert_actions_schema(db_path)

    down = _run_alembic(db_path, "downgrade 0005")
    assert down.returncode == 0, down.stderr
    assert not _table_exists(db_path, "actions")

    up_again = _run_alembic(db_path, "upgrade 0006")
    assert up_again.returncode == 0, up_again.stderr
    _assert_actions_schema(db_path)

import os
import shutil
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    result = subprocess.run(
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
    return result


def test_migration_applies_cleanly(tmp_path):
    db_path = tmp_path / "test.db"
    result = _run_alembic(db_path, "upgrade head")
    assert result.returncode == 0, result.stderr
    assert db_path.exists()


def test_migration_roundtrip(tmp_path):
    """upgrade head then downgrade base should both succeed."""
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr
    down = _run_alembic(db_path, "downgrade base")
    assert down.returncode == 0, down.stderr


def test_default_sqlite_parent_dir_is_created_for_alembic_check(tmp_path):
    cwd = Path(__file__).resolve().parents[2]
    data_dir = cwd / "data"
    backup_dir = tmp_path / "data-backup"

    if data_dir.exists():
        shutil.move(data_dir, backup_dir)
    try:
        result = subprocess.run(
            ["uv", "run", "alembic", "check"],
            capture_output=True,
            text=True,
            cwd=cwd,
            env={**os.environ},
        )
        assert "unable to open database file" not in result.stderr
        assert data_dir.exists()
    finally:
        if data_dir.exists():
            shutil.rmtree(data_dir)
        if backup_dir.exists():
            shutil.move(backup_dir, data_dir)

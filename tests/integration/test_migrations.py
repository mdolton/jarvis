import os
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

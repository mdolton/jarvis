"""Migration 0012: repair mcp_connections ids written in the wrong string format.

Migration 0011 inserted connection ids via ``str(uuid.uuid4())`` — a 36-char
dashed string. SQLAlchemy's ``Uuid`` type stores/queries UUIDs on SQLite as
32-char hex *without* dashes, so every primary-key lookup (``session.get``)
missed those rows: full-table scans parsed them fine, but ``MCPConnectionRepo.get``
returned ``None`` — surfacing as "no connection row" on every OAuth token refresh.

These tests pin both halves of the fix: 0011 now writes the canonical hex form,
and 0012 normalizes any dashed ids left behind in already-migrated databases.
"""

import os
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import create_engine, session_factory


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["uv", "run", "alembic", "-x", f"db_url=sqlite+aiosqlite:///{db_path}", *cmd.split()],
        capture_output=True, text=True, cwd=cwd, env={**os.environ},
    )


def _seed_dashed_connection(db_path: Path) -> uuid.UUID:
    """Insert a connection row exactly as the buggy 0011 did: a dashed-string id."""
    cid = uuid.uuid4()
    now = datetime.now(UTC).isoformat(sep=" ")
    conn = sqlite3.connect(db_path)
    # The 'calendar' provider is already seeded by migration 0011.
    conn.execute(
        "INSERT INTO mcp_connections (id, provider_key, label, runtime_name, enabled, scopes, "
        "scopes_granted, status, created_at, updated_at) VALUES "
        "(?, 'calendar', 'Default', 'calendar:default', 1, '[]', '[]', 'connected', ?, ?)",
        (str(cid), now, now),  # str(uuid) => 36-char dashed, the bug
    )
    conn.commit()
    conn.close()
    return cid


async def _orm_pk_lookup_succeeds(db_path: Path, cid: uuid.UUID) -> bool:
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with session_factory(engine)() as s:
            return await MCPConnectionRepo(s).get(cid) is not None
    finally:
        await engine.dispose()


async def test_0012_normalizes_dashed_connection_ids(tmp_path):
    db_path = tmp_path / "test.db"
    assert _run_alembic(db_path, "upgrade 0011").returncode == 0
    cid = _seed_dashed_connection(db_path)

    # Before repair: the row exists but the ORM primary-key lookup can't find it.
    assert await _orm_pk_lookup_succeeds(db_path, cid) is False

    assert _run_alembic(db_path, "upgrade 0012").returncode == 0

    # Stored id is now the canonical 32-char hex form...
    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT id FROM mcp_connections").fetchone()[0]
    conn.close()
    assert stored == cid.hex
    assert "-" not in stored

    # ...and the PK lookup (the OAuth-refresh path) now succeeds.
    assert await _orm_pk_lookup_succeeds(db_path, cid) is True


async def test_0011_writes_canonical_hex_ids(tmp_path):
    """A fresh 0011 run migrates a legacy oauth_credentials row into a connection
    whose id is the canonical 32-char hex (so the PK lookup works without 0012)."""
    db_path = tmp_path / "test.db"
    assert _run_alembic(db_path, "upgrade 0010").returncode == 0
    now = datetime.now(UTC).isoformat(sep=" ")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO oauth_credentials (provider_key, client_id_enc, access_token_enc, "
        "token_expires_at, scopes_granted, status, connected_at, updated_at) VALUES "
        "('calendar', X'01', X'02', ?, '[]', 'connected', ?, ?)",
        (now, now, now),
    )
    conn.commit()
    conn.close()

    assert _run_alembic(db_path, "upgrade 0011").returncode == 0

    conn = sqlite3.connect(db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM mcp_connections").fetchall()]
    conn.close()
    assert ids, "migration did not create a connection from the legacy row"
    for stored in ids:
        assert len(stored) == 32 and "-" not in stored, f"non-canonical id: {stored!r}"

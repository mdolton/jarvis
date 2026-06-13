"""0011 creates provider/connection tables, seeds builtin providers, migrates oauth_credentials."""

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

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


def test_0011_creates_tables_and_seeds_providers(tmp_path):
    db_path = tmp_path / "test.db"
    env = {**os.environ, "JARVIS_SECRETS_KEY": generate_key()}
    r = _run_alembic(db_path, "upgrade 0011", env=env)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert {"mcp_providers", "mcp_connections", "mcp_pending"} <= tables
    assert "oauth_credentials" not in tables
    cur.execute("SELECT key FROM mcp_providers ORDER BY key")
    assert {r[0] for r in cur.fetchall()} == {"calendar", "fastmail", "gmail"}
    conn.close()


def test_0011_migrates_existing_oauth_credentials_to_connection(tmp_path):
    db_path = tmp_path / "test.db"
    key = generate_key()
    env = {**os.environ, "JARVIS_SECRETS_KEY": key}
    # Build schema up to 0010, insert a legacy oauth_credentials row, then upgrade to 0011.
    assert _run_alembic(db_path, "upgrade 0010", env=env).returncode == 0
    f = Fernet(key.encode())
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.now(UTC).isoformat()
    cur.execute(
        "INSERT INTO oauth_credentials (provider_key, client_id_enc, client_secret_enc, "
        "access_token_enc, refresh_token_enc, token_expires_at, scopes_granted, status, "
        "last_error, connected_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "gmail",
            f.encrypt(b"CID"),
            f.encrypt(b"SEC"),
            f.encrypt(b"AT"),
            f.encrypt(b"RT"),
            now,
            "[]",
            "connected",
            None,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()

    assert _run_alembic(db_path, "upgrade 0011", env=env).returncode == 0
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT provider_key, label, runtime_name, client_id_enc, access_token_enc, status "
        "FROM mcp_connections"
    )
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1
    pk, label, rt, cid_enc, at_enc, status = rows[0]
    assert pk == "gmail" and rt == "gmail:default" and status == "connected"
    assert f.decrypt(cid_enc) == b"CID" and f.decrypt(at_enc) == b"AT"


def test_0011_env_import_creates_google_connections_when_no_legacy(tmp_path):
    """Env-only path: no legacy oauth_credentials; env vars create gmail+calendar connections."""
    db_path = tmp_path / "test.db"
    key = generate_key()
    env = {
        **os.environ,
        "JARVIS_SECRETS_KEY": key,
        "GOOGLE_OAUTH_CLIENT_ID": "env-cid",
        "GOOGLE_OAUTH_CLIENT_SECRET": "env-sec",
    }
    assert _run_alembic(db_path, "upgrade 0010", env=env).returncode == 0
    assert _run_alembic(db_path, "upgrade 0011", env=env).returncode == 0

    f = Fernet(key.encode())
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT runtime_name, client_id_enc, client_secret_enc, status FROM mcp_connections"
    )
    rows = {row[0]: row for row in cur.fetchall()}
    conn.close()

    assert "gmail:default" in rows, "gmail:default connection missing"
    assert "calendar:default" in rows, "calendar:default connection missing"

    for rt in ("gmail:default", "calendar:default"):
        _, cid_enc, sec_enc, status = rows[rt]
        assert status == "disconnected", f"{rt} status should be 'disconnected', got {status!r}"
        assert f.decrypt(cid_enc) == b"env-cid", f"{rt} client_id_enc mismatch"
        assert f.decrypt(sec_enc) == b"env-sec", f"{rt} client_secret_enc mismatch"


def test_0011_env_import_does_not_clobber_legacy_client(tmp_path):
    """Env vars must NOT overwrite a client_id that came from a legacy oauth_credentials row."""
    db_path = tmp_path / "test.db"
    key = generate_key()
    env = {
        **os.environ,
        "JARVIS_SECRETS_KEY": key,
        "GOOGLE_OAUTH_CLIENT_ID": "env-cid",
        "GOOGLE_OAUTH_CLIENT_SECRET": "env-sec",
    }
    assert _run_alembic(db_path, "upgrade 0010", env=env).returncode == 0

    f = Fernet(key.encode())
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO oauth_credentials (provider_key, client_id_enc, client_secret_enc, "
        "access_token_enc, refresh_token_enc, token_expires_at, scopes_granted, status, "
        "last_error, connected_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "gmail",
            f.encrypt(b"LEGACY-CID"),
            f.encrypt(b"LEGACY-SEC"),
            f.encrypt(b"AT"),
            f.encrypt(b"RT"),
            now,
            "[]",
            "connected",
            None,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()

    assert _run_alembic(db_path, "upgrade 0011", env=env).returncode == 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT runtime_name, client_id_enc FROM mcp_connections"
    )
    rows = {row[0]: row for row in cur.fetchall()}
    conn.close()

    # gmail came from the legacy row — its client must NOT be overwritten by env-cid.
    assert "gmail:default" in rows
    _, gmail_cid_enc = rows["gmail:default"]
    assert f.decrypt(gmail_cid_enc) == b"LEGACY-CID", (
        "Legacy client_id was clobbered by env var — no-clobber logic is broken"
    )

    # calendar had no legacy row, so it should get the env client.
    assert "calendar:default" in rows
    _, cal_cid_enc = rows["calendar:default"]
    assert f.decrypt(cal_cid_enc) == b"env-cid", (
        "calendar:default should have received env client_id"
    )


def test_0011_migrates_multiple_legacy_rows(tmp_path):
    """Both gmail and calendar legacy rows are migrated as separate mcp_connections."""
    db_path = tmp_path / "test.db"
    key = generate_key()
    # No Google env vars — only the secrets key so encryption works.
    env = {**os.environ, "JARVIS_SECRETS_KEY": key}
    assert _run_alembic(db_path, "upgrade 0010", env=env).returncode == 0

    f = Fernet(key.encode())
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for provider_key, at_plaintext in (("gmail", b"GMAIL-AT"), ("calendar", b"CAL-AT")):
        cur.execute(
            "INSERT INTO oauth_credentials (provider_key, client_id_enc, client_secret_enc, "
            "access_token_enc, refresh_token_enc, token_expires_at, scopes_granted, status, "
            "last_error, connected_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                provider_key,
                f.encrypt(b"CID"),
                f.encrypt(b"SEC"),
                f.encrypt(at_plaintext),
                f.encrypt(b"RT"),
                now,
                "[]",
                "connected",
                None,
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()

    assert _run_alembic(db_path, "upgrade 0011", env=env).returncode == 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT runtime_name, access_token_enc FROM mcp_connections")
    rows = {row[0]: row for row in cur.fetchall()}
    conn.close()

    assert set(rows) == {"gmail:default", "calendar:default"}, (
        f"Expected exactly gmail:default and calendar:default, got {set(rows)}"
    )
    assert f.decrypt(rows["gmail:default"][1]) == b"GMAIL-AT"
    assert f.decrypt(rows["calendar:default"][1]) == b"CAL-AT"

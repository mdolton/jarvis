# OAuth MCP Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable OAuth-protected HTTP MCP servers (Fastmail v1) to be connected, monitored, and disconnected from the dashboard. The framework supports DCR (RFC 7591) end-to-end with manual-mode left as a typed seam.

**Architecture:** A new `jarvis/oauth/` package owns the OAuth client (`flow.py`), built-in provider catalog (`catalog.py`), and DB-backed token store (`store.py`). `MCPManager` is refactored to per-server exit stacks so a single OAuth server can be torn down/rebuilt on token refresh without disturbing other servers. New dashboard route `/oauth/...` runs the consent dance; `/mcp` page is extended with Connect/Disconnect cards. Two new APScheduler jobs handle proactive token refresh and pending-state cleanup.

**Tech Stack:** Python 3.12, `httpx` (new dep, transitive via SDK), `cryptography` (new dep, for Fernet), SQLAlchemy 2.x async, Alembic, FastAPI, Jinja2, APScheduler, pytest with `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-04-25-oauth-mcp-management-design.md`. When in doubt, the spec is the source of truth.

---

## File Structure

| Path | New / Modified | Purpose |
|---|---|---|
| `jarvis/oauth/__init__.py` | new | package marker |
| `jarvis/oauth/crypto.py` | new | Fernet wrapper: load key from env, encrypt/decrypt bytes |
| `jarvis/oauth/catalog.py` | new | `OAUTH_CATALOG`, `ProviderEntry`, `AuthMode` |
| `jarvis/oauth/flow.py` | new | `OAuthFlow` class: discovery, DCR, start_authorization, handle_callback, refresh, revoke |
| `jarvis/oauth/pkce.py` | new | PKCE verifier/challenge + url-safe state generators |
| `jarvis/oauth/store.py` | new | `OAuthCredentialsRepo`, `OAuthPendingRepo` |
| `jarvis/persistence/models.py` | modified | add `OAuthCredentialsRow`, `OAuthPendingRow` |
| `alembic/versions/0003_oauth_tables.py` | new | DDL for both tables |
| `jarvis/mcp/manager.py` | modified | per-server exit stacks; dict-based `_sdk_servers`; new `replace_oauth_server`, `remove_oauth_server`; bootstrap iteration of catalog |
| `jarvis/config/schema.py` | modified | add `JARVIS_BASE_URL`, `JARVIS_SECRETS_KEY` (env-only, not YAML) — actually loaded by `loader.py` since they're env vars |
| `jarvis/config/loader.py` | modified | read `JARVIS_BASE_URL`, `JARVIS_SECRETS_KEY` env vars; expose on `LoadedConfig` |
| `jarvis/core/types.py` | modified | extend `AuditEventType` with the `oauth.*` event types |
| `jarvis/web/routes/oauth.py` | new | `GET /oauth/connect/{provider}`, `GET /oauth/callback`, `POST /oauth/disconnect/{provider}` |
| `jarvis/web/routes/mcp.py` | modified | join catalog × `oauth_credentials` for /mcp render |
| `jarvis/web/templates/mcp.html` | modified | add OAuth Providers section above existing list |
| `jarvis/web/templates/oauth_callback.html` | new | three-state callback page |
| `jarvis/web/app.py` | modified | register oauth router |
| `jarvis/main.py` | modified | wire `OAuthFlow`, schedule the two new APScheduler jobs, run bootstrap inline-refresh |
| `jarvis/scheduler/oauth_jobs.py` | new | `oauth_token_refresh`, `oauth_pending_sweep` job functions |
| `pyproject.toml` | modified | add `cryptography>=42`, `httpx>=0.27` |
| `README.md` | modified | document `JARVIS_BASE_URL`, `JARVIS_SECRETS_KEY`, OAuth setup |
| `tests/unit/test_oauth_crypto.py` | new | Fernet roundtrip + key change |
| `tests/unit/test_oauth_pkce.py` | new | PKCE math, state randomness |
| `tests/unit/test_oauth_catalog.py` | new | catalog frozen-ness + lookup |
| `tests/integration/test_oauth_models.py` | new | ORM table presence + roundtrip |
| `tests/integration/test_oauth_repos.py` | new | repo CRUD |
| `tests/integration/test_oauth_flow.py` | new | discovery, DCR, start_authorization, callback, refresh, revoke (all via `MockTransport`) |
| `tests/integration/test_mcp_manager_oauth.py` | new | replace/remove_oauth_server, bootstrap iteration, refresh-failure-doesn't-break-other-servers |
| `tests/integration/test_web_oauth.py` | new | /oauth/connect, /oauth/callback, /oauth/disconnect, /mcp rendering |
| `tests/integration/test_oauth_jobs.py` | new | refresh job, sweep job |
| `tests/integration/test_oauth_migration.py` | new | migration up/down |

---

## Task 0: Branch setup

- [ ] **Step 1: Create a feature branch off main**

```bash
git checkout -b oauth-mcp-management
```

---

## Task 1: Add `cryptography` and `httpx` dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the two deps to the `[project]` table**

Open `pyproject.toml`. Inside the `dependencies = [...]` list, add `"cryptography>=42"` and `"httpx>=0.27"` alphabetically alongside the existing entries (`httpx` lands between `fastapi` and `jinja2`; `cryptography` lands near the top).

- [ ] **Step 2: Sync the lockfile**

```bash
uv lock
```

Expected: `uv.lock` updates with new entries for `cryptography` and `httpx`. No errors.

- [ ] **Step 3: Verify install**

```bash
uv sync
uv run python -c "import cryptography.fernet; import httpx; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "add cryptography and httpx dependencies"
```

---

## Task 2: Fernet crypto helper (`jarvis/oauth/crypto.py`)

**Files:**
- Create: `jarvis/oauth/__init__.py`
- Create: `jarvis/oauth/crypto.py`
- Test: `tests/unit/test_oauth_crypto.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_oauth_crypto.py`:

```python
"""Fernet wrapper: load key from env, roundtrip, key-change invalidates."""

import os

import pytest

from jarvis.oauth.crypto import (
    SecretsKeyMissing,
    decrypt_blob,
    encrypt_blob,
    generate_key,
    load_secrets_key,
)


def test_generate_key_returns_url_safe_44_byte_string():
    key = generate_key()
    assert isinstance(key, str)
    assert len(key) == 44  # Fernet keys are 32 raw bytes = 44 url-safe chars


def test_load_secrets_key_reads_env(monkeypatch):
    key = generate_key()
    monkeypatch.setenv("JARVIS_SECRETS_KEY", key)
    assert load_secrets_key() == key.encode()


def test_load_secrets_key_missing_raises(monkeypatch):
    monkeypatch.delenv("JARVIS_SECRETS_KEY", raising=False)
    with pytest.raises(SecretsKeyMissing):
        load_secrets_key()


def test_encrypt_decrypt_roundtrip():
    key = generate_key().encode()
    plaintext = b"my-access-token-abc"
    cipher = encrypt_blob(plaintext, key)
    assert cipher != plaintext
    assert decrypt_blob(cipher, key) == plaintext


def test_decrypt_with_different_key_fails():
    key_a = generate_key().encode()
    key_b = generate_key().encode()
    cipher = encrypt_blob(b"secret", key_a)
    with pytest.raises(Exception):  # InvalidToken from cryptography
        decrypt_blob(cipher, key_b)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_oauth_crypto.py -v
```

Expected: ImportError because `jarvis.oauth.crypto` doesn't exist yet.

- [ ] **Step 3: Implement the module**

Create `jarvis/oauth/__init__.py` as an empty file.

Create `jarvis/oauth/crypto.py`:

```python
"""Fernet wrapper used to encrypt OAuth tokens and client secrets at rest."""

import os

from cryptography.fernet import Fernet


class SecretsKeyMissing(RuntimeError):
    """JARVIS_SECRETS_KEY env var is unset."""


def generate_key() -> str:
    """Generate a fresh Fernet key as a url-safe string. For ops/setup only."""
    return Fernet.generate_key().decode()


def load_secrets_key() -> bytes:
    """Return the configured Fernet key as bytes. Raises if unset."""
    raw = os.environ.get("JARVIS_SECRETS_KEY")
    if not raw:
        raise SecretsKeyMissing(
            "JARVIS_SECRETS_KEY env var is required to encrypt OAuth credentials. "
            "Generate one with `python -c 'from jarvis.oauth.crypto import generate_key; print(generate_key())'`."
        )
    return raw.encode()


def encrypt_blob(plaintext: bytes, key: bytes) -> bytes:
    return Fernet(key).encrypt(plaintext)


def decrypt_blob(cipher: bytes, key: bytes) -> bytes:
    return Fernet(key).decrypt(cipher)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_oauth_crypto.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/__init__.py jarvis/oauth/crypto.py tests/unit/test_oauth_crypto.py
git commit -m "add Fernet crypto helper for OAuth secrets"
```

---

## Task 3: Wire `JARVIS_BASE_URL` and `JARVIS_SECRETS_KEY` into config

**Files:**
- Modify: `jarvis/config/schema.py`
- Modify: `jarvis/config/loader.py`
- Test: `tests/unit/test_config_loader.py` (extend)

- [ ] **Step 1: Inspect current loader and `LoadedConfig`**

```bash
grep -n "class LoadedConfig\|def load_config" jarvis/config/loader.py
```

Read the function signature so the new fields slot in cleanly.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_config_loader.py`:

```python
def test_load_config_reads_base_url_and_secrets_key(tmp_path, monkeypatch, _write_minimal_configs):
    _write_minimal_configs(tmp_path)
    monkeypatch.setenv("JARVIS_BASE_URL", "https://jarvis.example.com")
    monkeypatch.setenv("JARVIS_SECRETS_KEY", "test-key-44-chars-long-padding-padding-pa==")
    cfg = load_config(tmp_path)
    assert cfg.base_url == "https://jarvis.example.com"
    assert cfg.secrets_key == b"test-key-44-chars-long-padding-padding-pa=="


def test_load_config_base_url_defaults_to_localhost(tmp_path, monkeypatch, _write_minimal_configs):
    _write_minimal_configs(tmp_path)
    monkeypatch.delenv("JARVIS_BASE_URL", raising=False)
    monkeypatch.setenv("JARVIS_SECRETS_KEY", "test-key-44-chars-long-padding-padding-pa==")
    cfg = load_config(tmp_path)
    assert cfg.base_url == "http://localhost:8080"


def test_load_config_secrets_key_missing_raises(tmp_path, monkeypatch, _write_minimal_configs):
    _write_minimal_configs(tmp_path)
    monkeypatch.delenv("JARVIS_SECRETS_KEY", raising=False)
    with pytest.raises(Exception):
        load_config(tmp_path)
```

If the test file has no `_write_minimal_configs` fixture, add it at module scope (consult the existing tests in the same file to see how minimal configs are written there — the pattern is one `jarvis.yaml`, one `channels.yaml`, one `mcp-servers.yaml`).

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_config_loader.py -v
```

Expected: AttributeError on `cfg.base_url` / `cfg.secrets_key`.

- [ ] **Step 4: Update `LoadedConfig` and `load_config`**

In `jarvis/config/loader.py`, add `base_url: str` and `secrets_key: bytes` fields to `LoadedConfig`. In `load_config`, after the YAML parsing, read the env vars:

```python
import os
from jarvis.oauth.crypto import load_secrets_key

# inside load_config(), after loading the YAML configs:
base_url = os.environ.get("JARVIS_BASE_URL", "http://localhost:8080")
secrets_key = load_secrets_key()
```

Then pass both into the `LoadedConfig(...)` constructor.

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_config_loader.py -v
```

Expected: all pass, including the two new ones and the previously-existing ones.

- [ ] **Step 6: Commit**

```bash
git add jarvis/config/loader.py tests/unit/test_config_loader.py
git commit -m "load JARVIS_BASE_URL and JARVIS_SECRETS_KEY env vars"
```

---

## Task 4: Lock current `MCPManager` behavior with regression tests

This task is purely defensive: capture what works *today* before refactoring in Task 5.

**Files:**
- Test: `tests/integration/test_mcp_manager.py` (extend)

- [ ] **Step 1: Add regression assertions to existing test**

Open `tests/integration/test_mcp_manager.py`. Find the existing successful-connect test and, after it passes, add:

```python
async def test_agent_mcp_servers_returns_list_of_connected(engine_and_factory, test_server_script):
    engine, factory = engine_and_factory
    cfg = MCPServersConfig(servers=[
        MCPServerConfig(name="echo", transport="stdio", command=["python", str(test_server_script)]),
    ])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        servers = mgr.agent_mcp_servers()
        assert len(servers) == 1
        # Stable identity: the same call returns the same SDK objects.
        assert mgr.agent_mcp_servers()[0] is servers[0]
    finally:
        await mgr.stop()


async def test_stop_closes_all_servers(engine_and_factory, test_server_script):
    engine, factory = engine_and_factory
    cfg = MCPServersConfig(servers=[
        MCPServerConfig(name="echo", transport="stdio", command=["python", str(test_server_script)]),
    ])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    await mgr.stop()
    # Idempotent: a second stop is a no-op.
    await mgr.stop()
```

- [ ] **Step 2: Run test to verify the existing behavior passes**

```bash
uv run pytest tests/integration/test_mcp_manager.py -v
```

Expected: all green. If the second test fails because `stop()` isn't idempotent, fix `stop()` to early-return when `self._stack` is None — that's a real bug worth fixing before the refactor.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_mcp_manager.py
git commit -m "lock current MCPManager behavior with regression tests"
```

---

## Task 5: Refactor `MCPManager` to per-server exit stacks

**Files:**
- Modify: `jarvis/mcp/manager.py`

- [ ] **Step 1: Read the current implementation top-to-bottom**

```bash
cat jarvis/mcp/manager.py
```

Internalize the structure. The refactor preserves every public method.

- [ ] **Step 2: Replace `_stack` and `_sdk_servers` with dicts**

Edit `jarvis/mcp/manager.py`:

Change the constructor:

```python
def __init__(
    self,
    *,
    config: MCPServersConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    self._config = config
    self._session_factory = session_factory
    self._stacks: dict[str, AsyncExitStack] = {}
    self._sdk_servers: dict[str, object] = {}
```

Update `_connect_one` to use per-server stacks:

```python
async def _connect_one(self, cfg: MCPServerConfig) -> None:
    async with self._session_factory() as session:
        row = await MCPServerRepo(session).upsert(name=cfg.name, transport=cfg.transport)
        server_id = row.id

    stack = AsyncExitStack()
    sdk_server = _build_sdk_server(cfg)
    await stack.enter_async_context(sdk_server)

    try:
        tools = await _list_tools(sdk_server)
    except Exception:
        await stack.aclose()
        raise

    self._stacks[cfg.name] = stack
    self._sdk_servers[cfg.name] = sdk_server

    async with self._session_factory() as session:
        srepo = MCPServerRepo(session)
        trepo = MCPToolRepo(session)
        await srepo.set_status(server_id, status="connected", last_error=None)
        await trepo.replace_for_server(server_id, tools=tools)
```

Update `stop`:

```python
async def stop(self) -> None:
    for name in list(self._stacks):
        try:
            await self._stacks[name].aclose()
        except Exception:
            _log.exception("error closing MCP server stack %r", name)
    self._stacks.clear()
    self._sdk_servers.clear()
```

Update `agent_mcp_servers`:

```python
def agent_mcp_servers(self) -> list[object]:
    return list(self._sdk_servers.values())
```

- [ ] **Step 3: Run regression tests**

```bash
uv run pytest tests/integration/test_mcp_manager.py -v
```

Expected: all green, including Task 4's new tests.

- [ ] **Step 4: Run full test suite to catch any other consumers of `_stack`**

```bash
uv run pytest -q
```

Expected: green or pre-existing failures only — no new failures from this refactor.

- [ ] **Step 5: Commit**

```bash
git add jarvis/mcp/manager.py
git commit -m "refactor MCPManager to per-server exit stacks"
```

---

## Task 6: Add `OAuthCredentialsRow` and `OAuthPendingRow` ORM models

**Files:**
- Modify: `jarvis/persistence/models.py`
- Test: `tests/integration/test_oauth_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_oauth_models.py`:

```python
"""ORM smoke for oauth_credentials and oauth_pending."""

from datetime import UTC, datetime

import pytest

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import OAuthCredentialsRow, OAuthPendingRow


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_oauth_credentials_roundtrip(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        row = OAuthCredentialsRow(
            provider_key="fastmail",
            client_id_enc=b"enc-client-id",
            client_secret_enc=b"enc-secret",
            access_token_enc=b"enc-access",
            refresh_token_enc=b"enc-refresh",
            token_expires_at=now,
            scopes_granted=["mail.read"],
            status="connected",
            last_error=None,
            connected_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.commit()

    async with factory() as session:
        from sqlalchemy import select
        got = (await session.execute(select(OAuthCredentialsRow))).scalar_one()
        assert got.provider_key == "fastmail"
        assert got.access_token_enc == b"enc-access"
        assert got.scopes_granted == ["mail.read"]
        assert got.status == "connected"


async def test_oauth_pending_roundtrip(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        row = OAuthPendingRow(
            state="abc123",
            provider_key="fastmail",
            code_verifier="verifier-xyz",
            created_at=now,
        )
        session.add(row)
        await session.commit()

    async with factory() as session:
        from sqlalchemy import select
        got = (await session.execute(select(OAuthPendingRow))).scalar_one()
        assert got.state == "abc123"
        assert got.code_verifier == "verifier-xyz"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_models.py -v
```

Expected: ImportError on `OAuthCredentialsRow`.

- [ ] **Step 3: Add the models**

Append to `jarvis/persistence/models.py` (after `MCPToolRow`):

```python
class OAuthCredentialsRow(Base):
    __tablename__ = "oauth_credentials"

    provider_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id_enc: Mapped[bytes] = mapped_column()
    client_secret_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    access_token_enc: Mapped[bytes] = mapped_column()
    refresh_token_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    token_expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    scopes_granted: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="connected")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())


class OAuthPendingRow(Base):
    __tablename__ = "oauth_pending"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
```

You may need to import `LargeBinary` if your SQLAlchemy version doesn't accept `Mapped[bytes]` directly. If `bytes` works (it does in 2.x), use it. Otherwise, replace `Mapped[bytes]` with `Mapped[bytes] = mapped_column(LargeBinary)` and import `from sqlalchemy import LargeBinary`.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_models.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/models.py tests/integration/test_oauth_models.py
git commit -m "add OAuthCredentialsRow and OAuthPendingRow ORM models"
```

---

## Task 7: Alembic migration for OAuth tables

**Files:**
- Create: `alembic/versions/0003_oauth_tables.py`
- Test: `tests/integration/test_oauth_migration.py`

- [ ] **Step 1: Inspect the previous migration for naming/style**

```bash
ls alembic/versions/
cat alembic/versions/0002_conv_composite_index.py
```

Confirm the revision id pattern (`0003`), the `down_revision = "0002"` link, and the import style.

- [ ] **Step 2: Write the failing test**

Create `tests/integration/test_oauth_migration.py`:

```python
"""Alembic migration 0003: oauth_credentials + oauth_pending."""

import subprocess
import sys
from pathlib import Path


def test_migrate_up_creates_oauth_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    db_url = f"sqlite:///{db_path}"
    repo_root = Path(__file__).resolve().parents[2]

    env = {
        "PATH": __import__("os").environ["PATH"],
        "JARVIS_DB_URL": db_url,
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        check=True,
    )

    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert "oauth_credentials" in tables
    assert "oauth_pending" in tables

    cur.execute("PRAGMA table_info('oauth_credentials')")
    cols = {row[1] for row in cur.fetchall()}
    expected = {
        "provider_key", "client_id_enc", "client_secret_enc",
        "access_token_enc", "refresh_token_enc", "token_expires_at",
        "scopes_granted", "status", "last_error", "connected_at", "updated_at",
    }
    assert expected.issubset(cols)
    conn.close()
```

If `JARVIS_DB_URL` isn't how `alembic/env.py` reads the URL, peek at `alembic/env.py` and adjust the env var name.

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_migration.py -v
```

Expected: tables `oauth_credentials` and `oauth_pending` not found.

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0003_oauth_tables.py`:

```python
"""oauth tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_credentials",
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("client_id_enc", sa.LargeBinary(), nullable=False),
        sa.Column("client_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=False),
        sa.Column("scopes_granted", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("provider_key"),
    )
    op.create_table(
        "oauth_pending",
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )


def downgrade() -> None:
    op.drop_table("oauth_pending")
    op.drop_table("oauth_credentials")
```

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_migration.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0003_oauth_tables.py tests/integration/test_oauth_migration.py
git commit -m "add Alembic migration for oauth tables"
```

---

## Task 8: `OAuthCredentialsRepo` and `OAuthPendingRepo`

**Files:**
- Create: `jarvis/oauth/store.py`
- Test: `tests/integration/test_oauth_repos.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_oauth_repos.py`:

```python
"""OAuth repos: CRUD + filter helpers."""

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_credentials_upsert_and_get(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid",
            client_secret_enc=b"cs",
            access_token_enc=b"at",
            refresh_token_enc=b"rt",
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=["s1"],
        )
        got = await repo.get("fastmail")
        assert got is not None
        assert got.access_token_enc == b"at"
        assert got.status == "connected"


async def test_credentials_set_status(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid",
            client_secret_enc=None,
            access_token_enc=b"at",
            refresh_token_enc=b"rt",
            token_expires_at=now,
            scopes_granted=[],
        )
        await repo.set_status("fastmail", status="needs_reauth", last_error="invalid_grant")
        got = await repo.get("fastmail")
        assert got.status == "needs_reauth"
        assert got.last_error == "invalid_grant"


async def test_credentials_delete(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid",
            client_secret_enc=None,
            access_token_enc=b"at",
            refresh_token_enc=None,
            token_expires_at=now,
            scopes_granted=[],
        )
        await repo.delete("fastmail")
        assert await repo.get("fastmail") is None


async def test_credentials_list_due_for_refresh(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="soon",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at", refresh_token_enc=b"rt",
            token_expires_at=now + timedelta(seconds=30),  # within 90s window
            scopes_granted=[],
        )
        await repo.upsert(
            provider_key="later",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at", refresh_token_enc=b"rt",
            token_expires_at=now + timedelta(hours=1),  # not yet due
            scopes_granted=[],
        )
        due = await repo.list_due_for_refresh(now=now, skew_seconds=90)
        keys = {row.provider_key for row in due}
        assert keys == {"soon"}


async def test_pending_insert_lookup_delete(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthPendingRepo(session)
        await repo.insert(state="s1", provider_key="fastmail", code_verifier="v1", now=now)
        got = await repo.get("s1")
        assert got is not None
        assert got.code_verifier == "v1"
        await repo.delete("s1")
        assert await repo.get("s1") is None


async def test_pending_sweep_expired(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthPendingRepo(session)
        await repo.insert(state="old", provider_key="fastmail", code_verifier="v",
                          now=now - timedelta(minutes=20))
        await repo.insert(state="new", provider_key="fastmail", code_verifier="v",
                          now=now - timedelta(seconds=30))
        deleted = await repo.sweep_expired(now=now, ttl_seconds=600)
        assert deleted == 1
        assert await repo.get("old") is None
        assert await repo.get("new") is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_repos.py -v
```

Expected: ImportError on `jarvis.oauth.store`.

- [ ] **Step 3: Implement the repos**

Create `jarvis/oauth/store.py`:

```python
"""Repositories for oauth_credentials and oauth_pending tables."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.persistence.models import OAuthCredentialsRow, OAuthPendingRow


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OAuthCredentialsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, provider_key: str) -> OAuthCredentialsRow | None:
        result = await self._session.execute(
            select(OAuthCredentialsRow).where(OAuthCredentialsRow.provider_key == provider_key)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        provider_key: str,
        client_id_enc: bytes,
        client_secret_enc: bytes | None,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        token_expires_at: datetime,
        scopes_granted: list[str],
    ) -> OAuthCredentialsRow:
        existing = await self.get(provider_key)
        now = _utcnow()
        if existing is None:
            row = OAuthCredentialsRow(
                provider_key=provider_key,
                client_id_enc=client_id_enc,
                client_secret_enc=client_secret_enc,
                access_token_enc=access_token_enc,
                refresh_token_enc=refresh_token_enc,
                token_expires_at=token_expires_at,
                scopes_granted=scopes_granted,
                status="connected",
                last_error=None,
                connected_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            existing.client_id_enc = client_id_enc
            existing.client_secret_enc = client_secret_enc
            existing.access_token_enc = access_token_enc
            existing.refresh_token_enc = refresh_token_enc
            existing.token_expires_at = token_expires_at
            existing.scopes_granted = scopes_granted
            existing.status = "connected"
            existing.last_error = None
            existing.updated_at = now
            row = existing
        await self._session.commit()
        return row

    async def set_status(self, provider_key: str, *, status: str, last_error: str | None) -> None:
        row = await self.get(provider_key)
        if row is None:
            return
        row.status = status
        row.last_error = last_error
        row.updated_at = _utcnow()
        await self._session.commit()

    async def update_tokens(
        self,
        provider_key: str,
        *,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        token_expires_at: datetime,
    ) -> None:
        row = await self.get(provider_key)
        if row is None:
            raise LookupError(f"no oauth_credentials row for {provider_key!r}")
        row.access_token_enc = access_token_enc
        if refresh_token_enc is not None:
            row.refresh_token_enc = refresh_token_enc
        row.token_expires_at = token_expires_at
        row.status = "connected"
        row.last_error = None
        row.updated_at = _utcnow()
        await self._session.commit()

    async def delete(self, provider_key: str) -> None:
        await self._session.execute(
            delete(OAuthCredentialsRow).where(OAuthCredentialsRow.provider_key == provider_key)
        )
        await self._session.commit()

    async def list_all(self) -> list[OAuthCredentialsRow]:
        result = await self._session.execute(select(OAuthCredentialsRow))
        return list(result.scalars())

    async def list_due_for_refresh(
        self, *, now: datetime, skew_seconds: int = 90
    ) -> list[OAuthCredentialsRow]:
        threshold = now + timedelta(seconds=skew_seconds)
        result = await self._session.execute(
            select(OAuthCredentialsRow).where(
                OAuthCredentialsRow.status == "connected",
                OAuthCredentialsRow.token_expires_at <= threshold,
            )
        )
        return list(result.scalars())


class OAuthPendingRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self, *, state: str, provider_key: str, code_verifier: str, now: datetime
    ) -> None:
        self._session.add(
            OAuthPendingRow(
                state=state,
                provider_key=provider_key,
                code_verifier=code_verifier,
                created_at=now,
            )
        )
        await self._session.commit()

    async def get(self, state: str) -> OAuthPendingRow | None:
        result = await self._session.execute(
            select(OAuthPendingRow).where(OAuthPendingRow.state == state)
        )
        return result.scalar_one_or_none()

    async def delete(self, state: str) -> None:
        await self._session.execute(
            delete(OAuthPendingRow).where(OAuthPendingRow.state == state)
        )
        await self._session.commit()

    async def sweep_expired(self, *, now: datetime, ttl_seconds: int = 600) -> int:
        cutoff = now - timedelta(seconds=ttl_seconds)
        result = await self._session.execute(
            delete(OAuthPendingRow).where(OAuthPendingRow.created_at < cutoff)
        )
        await self._session.commit()
        return result.rowcount or 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_repos.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/store.py tests/integration/test_oauth_repos.py
git commit -m "add OAuthCredentialsRepo and OAuthPendingRepo"
```

---

## Task 9: Catalog module + collision check

**Files:**
- Create: `jarvis/oauth/catalog.py`
- Test: `tests/unit/test_oauth_catalog.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_oauth_catalog.py`:

```python
"""OAuth provider catalog: presence, types, collision check."""

import pytest

from jarvis.oauth.catalog import (
    OAUTH_CATALOG,
    AuthMode,
    ProviderEntry,
    assert_no_yaml_collision,
)


def test_fastmail_entry_present():
    entry = OAUTH_CATALOG["fastmail"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.DCR
    assert entry.mcp_url == "https://api.fastmail.com/mcp"
    assert entry.oauth_metadata_url is not None


def test_catalog_is_frozen_dict_of_frozen_entries():
    # Adding a new key at runtime is a programmer error — we don't enforce
    # a frozen dict, but each ProviderEntry is frozen.
    with pytest.raises(Exception):
        OAUTH_CATALOG["fastmail"].mcp_url = "x"  # frozen dataclass


def test_assert_no_yaml_collision_passes_with_disjoint_names():
    assert_no_yaml_collision(["filesystem", "remote-api"])  # does not raise


def test_assert_no_yaml_collision_raises_on_match():
    with pytest.raises(ValueError, match="fastmail"):
        assert_no_yaml_collision(["filesystem", "fastmail"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_oauth_catalog.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement the catalog**

Create `jarvis/oauth/catalog.py`:

```python
"""Built-in registry of OAuth-capable MCP providers.

Adding a provider is a typed PR with tests, never a config edit.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class AuthMode(StrEnum):
    DCR = "dcr"          # RFC 7591 dynamic client registration
    MANUAL = "manual"    # operator-supplied client_id/secret (not implemented in v1)


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    key: str
    display_name: str
    mcp_url: str
    auth_mode: AuthMode
    oauth_metadata_url: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()
    pkce: bool = True


OAUTH_CATALOG: dict[str, ProviderEntry] = {
    "fastmail": ProviderEntry(
        key="fastmail",
        display_name="Fastmail",
        mcp_url="https://api.fastmail.com/mcp",
        auth_mode=AuthMode.DCR,
        oauth_metadata_url="https://api.fastmail.com/.well-known/oauth-authorization-server",
        scopes=(),
    ),
}


def assert_no_yaml_collision(yaml_server_names: Iterable[str]) -> None:
    """Raise ValueError if any YAML-defined MCP server name matches a catalog key."""
    catalog_keys = set(OAUTH_CATALOG)
    yaml_set = set(yaml_server_names)
    overlap = catalog_keys & yaml_set
    if overlap:
        joined = ", ".join(sorted(overlap))
        raise ValueError(
            f"YAML MCP server name(s) collide with built-in OAuth catalog keys: {joined}. "
            f"Rename the YAML server(s)."
        )
```

- [ ] **Step 4: Wire the collision check into `MCPManager.start`**

Edit `jarvis/mcp/manager.py`. At the top of `start()`, before the loop:

```python
from jarvis.oauth.catalog import assert_no_yaml_collision

async def start(self) -> None:
    assert_no_yaml_collision(s.name for s in self._config.servers)
    for server_cfg in self._config.servers:
        ...
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/unit/test_oauth_catalog.py tests/integration/test_mcp_manager.py -v
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add jarvis/oauth/catalog.py jarvis/mcp/manager.py tests/unit/test_oauth_catalog.py
git commit -m "add OAuth provider catalog with collision check"
```

---

## Task 10: PKCE + state generators

**Files:**
- Create: `jarvis/oauth/pkce.py`
- Test: `tests/unit/test_oauth_pkce.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_oauth_pkce.py`:

```python
"""PKCE and state generators."""

import base64
import hashlib

from jarvis.oauth.pkce import generate_code_challenge, generate_code_verifier, generate_state


def test_verifier_url_safe_and_long():
    v = generate_code_verifier()
    assert 43 <= len(v) <= 128
    assert all(c.isalnum() or c in "-._~" for c in v)


def test_challenge_is_sha256_of_verifier_base64url_no_padding():
    v = "test-verifier"
    c = generate_code_challenge(v)
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert c == expected


def test_state_is_url_safe_and_random():
    s1 = generate_state()
    s2 = generate_state()
    assert s1 != s2
    assert len(s1) >= 32
    assert all(c.isalnum() or c in "-_" for c in s1)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_oauth_pkce.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Create `jarvis/oauth/pkce.py`:

```python
"""PKCE (RFC 7636) and OAuth state helpers."""

import base64
import hashlib
import secrets


def generate_code_verifier(*, length: int = 64) -> str:
    """Return a high-entropy url-safe verifier string of `length` chars."""
    if not (43 <= length <= 128):
        raise ValueError("PKCE verifier length must be 43..128 per RFC 7636")
    # token_urlsafe yields ~1.3 chars per byte; ask for enough bytes to cover length.
    raw = secrets.token_urlsafe((length * 3) // 4 + 1)
    return raw[:length]


def generate_code_challenge(verifier: str) -> str:
    """S256 challenge: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state(*, n_bytes: int = 32) -> str:
    """Url-safe random state token, ~43 chars for n_bytes=32."""
    return secrets.token_urlsafe(n_bytes)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_oauth_pkce.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/pkce.py tests/unit/test_oauth_pkce.py
git commit -m "add PKCE and state generators"
```

---

## Task 11: Add OAuth audit event types

**Files:**
- Modify: `jarvis/core/types.py`

- [ ] **Step 1: Extend `AuditEventType`**

In `jarvis/core/types.py`, add to `AuditEventType`:

```python
class AuditEventType(StrEnum):
    # ... existing entries unchanged ...
    OAUTH_DISCOVERY_STARTED = "oauth.discovery_started"
    OAUTH_DISCOVERY_SUCCEEDED = "oauth.discovery_succeeded"
    OAUTH_DISCOVERY_FAILED = "oauth.discovery_failed"
    OAUTH_DCR_REGISTERED = "oauth.dcr_registered"
    OAUTH_CONSENT_REDIRECT_ISSUED = "oauth.consent_redirect_issued"
    OAUTH_CALLBACK_RECEIVED = "oauth.callback_received"
    OAUTH_STATE_MISMATCH = "oauth.state_mismatch"
    OAUTH_CONSENT_DECLINED = "oauth.consent_declined"
    OAUTH_TOKENS_OBTAINED = "oauth.tokens_obtained"
    OAUTH_REFRESH_SUCCEEDED = "oauth.refresh_succeeded"
    OAUTH_REFRESH_TRANSIENT_FAILURE = "oauth.refresh_transient_failure"
    OAUTH_REFRESH_PERMANENTLY_FAILED = "oauth.refresh_permanently_failed"
    OAUTH_REVOKED = "oauth.revoked"
```

- [ ] **Step 2: Run existing tests to ensure no breakage**

```bash
uv run pytest tests/unit/test_core_types.py -v
```

Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add jarvis/core/types.py
git commit -m "add oauth.* audit event types"
```

---

## Task 12: `OAuthFlow.discover` (RFC 8414 metadata)

The flow class spans Tasks 12–17. We build it incrementally with one method per task and tests for each.

**Files:**
- Create: `jarvis/oauth/flow.py`
- Test: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_oauth_flow.py`:

```python
"""OAuthFlow tests using httpx.MockTransport (no real network)."""

import httpx
import pytest

from jarvis.oauth.catalog import AuthMode, OAUTH_CATALOG, ProviderEntry
from jarvis.oauth.flow import OAuthDiscoveryError, OAuthFlow


@pytest.fixture
def fastmail_metadata_payload():
    return {
        "issuer": "https://api.fastmail.com",
        "authorization_endpoint": "https://api.fastmail.com/oauth/authorize",
        "token_endpoint": "https://api.fastmail.com/oauth/token",
        "registration_endpoint": "https://api.fastmail.com/oauth/register",
        "revocation_endpoint": "https://api.fastmail.com/oauth/revoke",
        "code_challenge_methods_supported": ["S256"],
    }


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_discover_parses_metadata(fastmail_metadata_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-authorization-server"
        return httpx.Response(200, json=fastmail_metadata_payload)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
    assert metadata.authorization_endpoint == "https://api.fastmail.com/oauth/authorize"
    assert metadata.registration_endpoint == "https://api.fastmail.com/oauth/register"
    assert "S256" in metadata.code_challenge_methods_supported


async def test_discover_rejects_missing_s256(fastmail_metadata_payload):
    fastmail_metadata_payload["code_challenge_methods_supported"] = ["plain"]
    def handler(request):
        return httpx.Response(200, json=fastmail_metadata_payload)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    with pytest.raises(OAuthDiscoveryError, match="S256"):
        await flow.discover(OAUTH_CATALOG["fastmail"])


async def test_discover_5xx_raises_discovery_error():
    def handler(request):
        return httpx.Response(503, text="upstream down")
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    with pytest.raises(OAuthDiscoveryError):
        await flow.discover(OAUTH_CATALOG["fastmail"])
```

The `secrets_key` is a real Fernet key in later tests; here we never decrypt so a placeholder bytes value is fine.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: ImportError on `jarvis.oauth.flow`.

- [ ] **Step 3: Implement `OAuthFlow` skeleton + `discover`**

Create `jarvis/oauth/flow.py`:

```python
"""OAuth client used by the dashboard. State machine across discover, register,
authorize, exchange, refresh, revoke. All HTTP via injected httpx.AsyncClient
so unit tests can stub responses with MockTransport."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from jarvis.oauth.catalog import AuthMode, ProviderEntry


class OAuthDiscoveryError(RuntimeError):
    """Raised when RFC 8414 metadata fetch or validation fails."""


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    code_challenge_methods_supported: list[str]


class OAuthFlow:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession] | None,
        base_url: str,
        secrets_key: bytes,
    ) -> None:
        self._http = http_client
        self._session_factory = session_factory
        self._base_url = base_url.rstrip("/")
        self._secrets_key = secrets_key

    @property
    def redirect_uri(self) -> str:
        return f"{self._base_url}/oauth/callback"

    async def discover(self, entry: ProviderEntry) -> ProviderMetadata:
        if entry.auth_mode is not AuthMode.DCR:
            raise NotImplementedError(
                f"Manual-mode OAuth not yet supported for provider {entry.key!r}"
            )
        if entry.oauth_metadata_url is None:
            raise OAuthDiscoveryError(f"{entry.key}: no oauth_metadata_url configured")
        try:
            resp = await self._http.get(entry.oauth_metadata_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise OAuthDiscoveryError(f"{entry.key}: metadata fetch failed: {e}") from e
        try:
            data = resp.json()
        except Exception as e:
            raise OAuthDiscoveryError(f"{entry.key}: metadata not JSON") from e
        try:
            metadata = ProviderMetadata(
                authorization_endpoint=data["authorization_endpoint"],
                token_endpoint=data["token_endpoint"],
                registration_endpoint=data.get("registration_endpoint"),
                revocation_endpoint=data.get("revocation_endpoint"),
                code_challenge_methods_supported=list(
                    data.get("code_challenge_methods_supported", [])
                ),
            )
        except KeyError as e:
            raise OAuthDiscoveryError(f"{entry.key}: metadata missing field {e}") from e
        if "S256" not in metadata.code_challenge_methods_supported:
            raise OAuthDiscoveryError(
                f"{entry.key}: provider does not advertise S256 PKCE method"
            )
        return metadata
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "add OAuthFlow.discover for RFC 8414 metadata"
```

---

## Task 13: `OAuthFlow.register_client` (DCR)

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Modify: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_oauth_flow.py`:

```python
async def test_register_client_dcr_returns_client_id(fastmail_metadata_payload):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".well-known/oauth-authorization-server"):
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            captured["body"] = request.read().decode()
            return httpx.Response(201, json={"client_id": "abc", "client_secret": "shh"})
        return httpx.Response(404)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
    creds = await flow.register_client(OAUTH_CATALOG["fastmail"], metadata)
    assert creds.client_id == "abc"
    assert creds.client_secret == "shh"
    import json
    body = json.loads(captured["body"])
    assert body["redirect_uris"] == ["http://localhost:8080/oauth/callback"]
    assert "authorization_code" in body["grant_types"]


async def test_register_client_no_secret_returned_means_public(fastmail_metadata_payload):
    def handler(request):
        if request.url.path.endswith(".well-known/oauth-authorization-server"):
            return httpx.Response(200, json=fastmail_metadata_payload)
        return httpx.Response(201, json={"client_id": "pub-only"})
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
    creds = await flow.register_client(OAUTH_CATALOG["fastmail"], metadata)
    assert creds.client_id == "pub-only"
    assert creds.client_secret is None


async def test_register_client_no_endpoint_raises(fastmail_metadata_payload):
    fastmail_metadata_payload.pop("registration_endpoint")
    def handler(request):
        return httpx.Response(200, json=fastmail_metadata_payload)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=None,
                     base_url="http://localhost:8080", secrets_key=b"k")
    metadata = await flow.discover(OAUTH_CATALOG["fastmail"])
    from jarvis.oauth.flow import DCRUnsupportedError
    with pytest.raises(DCRUnsupportedError):
        await flow.register_client(OAUTH_CATALOG["fastmail"], metadata)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: ImportError on `register_client` / `DCRUnsupportedError`.

- [ ] **Step 3: Implement**

Add to `jarvis/oauth/flow.py`:

```python
class DCRUnsupportedError(RuntimeError):
    """Provider doesn't advertise a registration_endpoint."""


@dataclass(frozen=True, slots=True)
class RegisteredClient:
    client_id: str
    client_secret: str | None


# Inside OAuthFlow:
async def register_client(
    self, entry: ProviderEntry, metadata: ProviderMetadata
) -> RegisteredClient:
    if metadata.registration_endpoint is None:
        raise DCRUnsupportedError(
            f"{entry.key}: provider does not support DCR; manual-mode OAuth not yet implemented"
        )
    body = {
        "client_name": "Jarvis",
        "redirect_uris": [self.redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "client_secret_basic",
    }
    resp = await self._http.post(metadata.registration_endpoint, json=body)
    resp.raise_for_status()
    data = resp.json()
    return RegisteredClient(
        client_id=data["client_id"],
        client_secret=data.get("client_secret"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "add OAuthFlow.register_client (DCR)"
```

---

## Task 14: `OAuthFlow.start_authorization`

This composes discover + register-if-needed + PKCE + state insert + URL build.

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Modify: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_oauth_flow.py`:

```python
from urllib.parse import parse_qs, urlparse

from jarvis.oauth.crypto import generate_key
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest.fixture
async def db_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_start_authorization_first_time_registers_and_returns_consent_url(
    db_factory, fastmail_metadata_payload
):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid-1", "client_secret": "sec"})
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")

    parsed = urlparse(consent_url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "api.fastmail.com"
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cid-1"]
    assert qs["redirect_uri"] == ["http://localhost:8080/oauth/callback"]
    assert qs["code_challenge_method"] == ["S256"]
    state = qs["state"][0]

    # An oauth_pending row was inserted for the state.
    async with db_factory() as session:
        pending = await OAuthPendingRepo(session).get(state)
        assert pending is not None
        assert pending.provider_key == "fastmail"

    # Credentials row exists with the registered client_id (encrypted).
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        # Pre-token-exchange row may exist with empty access_token; check client_id was stored.
        # If the impl defers credentials insert until token exchange, no row here.
        # Both designs are acceptable per spec — assert presence of either pending or registered state.
        assert cred is None or cred.client_id_enc != b""


async def test_start_authorization_skips_register_if_client_already_known(
    db_factory, fastmail_metadata_payload
):
    register_calls = {"count": 0}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            register_calls["count"] += 1
            return httpx.Response(201, json={"client_id": "x", "client_secret": "y"})
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    await flow.start_authorization("fastmail")
    await flow.start_authorization("fastmail")
    assert register_calls["count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: AttributeError on `start_authorization`.

- [ ] **Step 3: Implement**

Add to `jarvis/oauth/flow.py`:

```python
from datetime import UTC, datetime
from urllib.parse import urlencode

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.crypto import encrypt_blob
from jarvis.oauth.pkce import (
    generate_code_challenge,
    generate_code_verifier,
    generate_state,
)
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo


# Inside OAuthFlow:
async def start_authorization(self, provider_key: str) -> str:
    if self._session_factory is None:
        raise RuntimeError("OAuthFlow needs a session_factory for start_authorization")
    entry = OAUTH_CATALOG[provider_key]
    metadata = await self.discover(entry)

    # Get-or-register the DCR client.
    async with self._session_factory() as session:
        existing = await OAuthCredentialsRepo(session).get(provider_key)

    if existing is None or not existing.client_id_enc:
        client = await self.register_client(entry, metadata)
        # Persist client_id (and optional secret). access_token is empty until callback.
        async with self._session_factory() as session:
            cid_enc = encrypt_blob(client.client_id.encode(), self._secrets_key)
            sec_enc = (
                encrypt_blob(client.client_secret.encode(), self._secrets_key)
                if client.client_secret
                else None
            )
            await OAuthCredentialsRepo(session).upsert(
                provider_key=provider_key,
                client_id_enc=cid_enc,
                client_secret_enc=sec_enc,
                access_token_enc=b"",
                refresh_token_enc=None,
                token_expires_at=datetime.now(UTC),
                scopes_granted=[],
            )
            # Brand-new credentials carry status=connected by repo default but no real
            # access_token — this is a transient state until the callback. We accept
            # this rather than introducing a fourth status; access_token_enc=b"" lets
            # MCPManager bootstrap skip until tokens land.
            client_id = client.client_id
    else:
        from jarvis.oauth.crypto import decrypt_blob
        client_id = decrypt_blob(existing.client_id_enc, self._secrets_key).decode()

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    state = generate_state()

    async with self._session_factory() as session:
        await OAuthPendingRepo(session).insert(
            state=state, provider_key=provider_key,
            code_verifier=verifier, now=datetime.now(UTC),
        )

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": self.redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if entry.scopes:
        params["scope"] = " ".join(entry.scopes)
    params.update(entry.extra_auth_params)
    return f"{metadata.authorization_endpoint}?{urlencode(params)}"
```

The bootstrap-skip-until-tokens-land comment captures a real subtlety: `access_token_enc=b""` is a sentinel for "registered but not authorized." `MCPManager` will check this and skip the SDK build. Document it inline.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "add OAuthFlow.start_authorization"
```

---

## Task 15: `OAuthFlow.handle_callback`

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Modify: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_oauth_flow.py`:

```python
async def test_handle_callback_happy_path(db_factory, fastmail_metadata_payload):
    state_seen = {}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            body = request.read().decode()
            state_seen["body"] = body
            return httpx.Response(200, json={
                "access_token": "AT", "refresh_token": "RT",
                "expires_in": 3600, "token_type": "Bearer",
                "scope": "mail.read",
            })
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    state = parse_qs(urlparse(consent_url).query)["state"][0]

    result = await flow.handle_callback(state=state, code="abc")
    assert result.provider_key == "fastmail"

    # The pending row was deleted.
    async with db_factory() as session:
        assert await OAuthPendingRepo(session).get(state) is None

    # Credentials updated with real tokens.
    from jarvis.oauth.crypto import decrypt_blob
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert decrypt_blob(cred.access_token_enc, key) == b"AT"
        assert decrypt_blob(cred.refresh_token_enc, key) == b"RT"
        assert cred.scopes_granted == ["mail.read"]


async def test_handle_callback_unknown_state_raises(db_factory, fastmail_metadata_payload):
    def handler(request):
        return httpx.Response(404)
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=generate_key().encode())
    from jarvis.oauth.flow import OAuthCallbackError
    with pytest.raises(OAuthCallbackError, match="state"):
        await flow.handle_callback(state="not-a-real-state", code="abc")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: AttributeError.

- [ ] **Step 3: Implement**

Add to `jarvis/oauth/flow.py`:

```python
import base64

from jarvis.oauth.crypto import decrypt_blob


class OAuthCallbackError(RuntimeError):
    """Callback failed validation or token exchange."""


@dataclass(frozen=True, slots=True)
class CallbackResult:
    provider_key: str
    scopes_granted: list[str]


# Inside OAuthFlow:
async def handle_callback(self, *, state: str, code: str) -> CallbackResult:
    if self._session_factory is None:
        raise RuntimeError("OAuthFlow needs a session_factory for handle_callback")

    async with self._session_factory() as session:
        pending = await OAuthPendingRepo(session).get(state)
    if pending is None:
        raise OAuthCallbackError(f"unknown or expired state {state!r}")

    provider_key = pending.provider_key
    entry = OAUTH_CATALOG[provider_key]
    metadata = await self.discover(entry)

    async with self._session_factory() as session:
        cred = await OAuthCredentialsRepo(session).get(provider_key)
    if cred is None or not cred.client_id_enc:
        raise OAuthCallbackError(f"{provider_key}: no registered client; cannot complete callback")

    client_id = decrypt_blob(cred.client_id_enc, self._secrets_key).decode()
    client_secret = (
        decrypt_blob(cred.client_secret_enc, self._secrets_key).decode()
        if cred.client_secret_enc
        else None
    )

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": self.redirect_uri,
        "code_verifier": pending.code_verifier,
    }
    headers: dict[str, str] = {}
    if client_secret is not None:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    else:
        form["client_id"] = client_id

    resp = await self._http.post(metadata.token_endpoint, data=form, headers=headers)
    if resp.status_code >= 400:
        raise OAuthCallbackError(
            f"token exchange returned {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()

    access_token = data["access_token"]
    refresh_token = data.get("refresh_token")
    expires_in = int(data.get("expires_in", 3600))
    scope = data.get("scope", "")
    scopes_granted = scope.split() if scope else []
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

    async with self._session_factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key=provider_key,
            client_id_enc=cred.client_id_enc,
            client_secret_enc=cred.client_secret_enc,
            access_token_enc=encrypt_blob(access_token.encode(), self._secrets_key),
            refresh_token_enc=encrypt_blob(refresh_token.encode(), self._secrets_key)
            if refresh_token else None,
            token_expires_at=expires_at,
            scopes_granted=scopes_granted,
        )
        await OAuthPendingRepo(session).delete(state)

    return CallbackResult(provider_key=provider_key, scopes_granted=scopes_granted)
```

Add the import at top: `from datetime import timedelta`.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "add OAuthFlow.handle_callback"
```

---

## Task 16: `OAuthFlow.refresh`

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Modify: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_oauth_flow.py`:

```python
async def test_refresh_happy_path(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            form = dict(p.split("=", 1) for p in request.read().decode().split("&"))
            if form["grant_type"] == "authorization_code":
                return httpx.Response(200, json={
                    "access_token": "AT", "refresh_token": "RT",
                    "expires_in": 3600, "token_type": "Bearer",
                })
            if form["grant_type"] == "refresh_token":
                return httpx.Response(200, json={
                    "access_token": "AT2", "refresh_token": "RT2",
                    "expires_in": 3600, "token_type": "Bearer",
                })
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=state, code="abc")

    new_headers = await flow.refresh("fastmail")
    assert new_headers["Authorization"] == "Bearer AT2"

    from jarvis.oauth.crypto import decrypt_blob
    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert decrypt_blob(cred.access_token_enc, key) == b"AT2"
        assert decrypt_blob(cred.refresh_token_enc, key) == b"RT2"


async def test_refresh_invalid_grant_marks_needs_reauth(db_factory, fastmail_metadata_payload):
    state = {"step": "authcode"}
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            if state["step"] == "authcode":
                state["step"] = "refresh"
                return httpx.Response(200, json={
                    "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                })
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    from jarvis.oauth.flow import OAuthRefreshPermanentError
    with pytest.raises(OAuthRefreshPermanentError):
        await flow.refresh("fastmail")

    async with db_factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert cred.status == "needs_reauth"
        assert "invalid_grant" in (cred.last_error or "")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: AttributeError on `refresh`.

- [ ] **Step 3: Implement**

Add to `jarvis/oauth/flow.py`:

```python
class OAuthRefreshTransientError(RuntimeError):
    pass


class OAuthRefreshPermanentError(RuntimeError):
    pass


# Inside OAuthFlow:
async def refresh(self, provider_key: str) -> dict[str, str]:
    """Refresh tokens. Returns new headers dict for MCPServerStreamableHttp.

    Raises OAuthRefreshTransientError on network/5xx (caller may retry).
    Raises OAuthRefreshPermanentError on invalid_grant or missing refresh token
    (caller marks needs_reauth — already done here for the latter).
    """
    if self._session_factory is None:
        raise RuntimeError("OAuthFlow needs a session_factory for refresh")
    entry = OAUTH_CATALOG[provider_key]
    metadata = await self.discover(entry)

    async with self._session_factory() as session:
        cred = await OAuthCredentialsRepo(session).get(provider_key)
    if cred is None:
        raise OAuthRefreshPermanentError(f"{provider_key}: no credentials row")
    if not cred.refresh_token_enc:
        await self._mark_needs_reauth(provider_key, "no refresh_token on file")
        raise OAuthRefreshPermanentError(f"{provider_key}: no refresh_token on file")

    client_id = decrypt_blob(cred.client_id_enc, self._secrets_key).decode()
    client_secret = (
        decrypt_blob(cred.client_secret_enc, self._secrets_key).decode()
        if cred.client_secret_enc else None
    )
    refresh_token = decrypt_blob(cred.refresh_token_enc, self._secrets_key).decode()

    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    headers: dict[str, str] = {}
    if client_secret is not None:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    else:
        form["client_id"] = client_id

    try:
        resp = await self._http.post(metadata.token_endpoint, data=form, headers=headers)
    except httpx.HTTPError as e:
        raise OAuthRefreshTransientError(f"network: {e}") from e

    if 500 <= resp.status_code < 600:
        raise OAuthRefreshTransientError(f"token endpoint {resp.status_code}")
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", "")
        except Exception:
            err = resp.text[:120]
        await self._mark_needs_reauth(provider_key, f"refresh failed: {err}")
        raise OAuthRefreshPermanentError(f"{provider_key}: refresh permanently failed: {err}")

    data = resp.json()
    access_token = data["access_token"]
    new_refresh = data.get("refresh_token")  # may rotate
    expires_in = int(data.get("expires_in", 3600))
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

    async with self._session_factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.update_tokens(
            provider_key,
            access_token_enc=encrypt_blob(access_token.encode(), self._secrets_key),
            refresh_token_enc=encrypt_blob(new_refresh.encode(), self._secrets_key)
            if new_refresh else None,
            token_expires_at=expires_at,
        )

    return {"Authorization": f"Bearer {access_token}"}


async def _mark_needs_reauth(self, provider_key: str, reason: str) -> None:
    async with self._session_factory() as session:
        await OAuthCredentialsRepo(session).set_status(
            provider_key, status="needs_reauth", last_error=reason
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "add OAuthFlow.refresh"
```

---

## Task 17: `OAuthFlow.revoke` and decrypted-headers helper

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Modify: `tests/integration/test_oauth_flow.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_oauth_flow.py`:

```python
async def test_revoke_calls_revocation_endpoint_and_deletes_credentials(
    db_factory, fastmail_metadata_payload
):
    revoke_calls = {"count": 0}

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={
                "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            })
        if request.url.path == "/oauth/revoke":
            revoke_calls["count"] += 1
            return httpx.Response(200)
        return httpx.Response(404)

    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    await flow.revoke("fastmail")
    assert revoke_calls["count"] >= 1
    async with db_factory() as session:
        assert await OAuthCredentialsRepo(session).get("fastmail") is None


async def test_revoke_silent_when_endpoint_5xx(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        if request.url.path == "/oauth/revoke":
            return httpx.Response(503)
        return httpx.Response(404)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")
    # 5xx must not raise — local cleanup proceeds.
    await flow.revoke("fastmail")
    async with db_factory() as session:
        assert await OAuthCredentialsRepo(session).get("fastmail") is None


async def test_current_headers_returns_bearer(db_factory, fastmail_metadata_payload):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata_payload)
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        return httpx.Response(404)
    key = generate_key().encode()
    flow = OAuthFlow(http_client=make_client(handler), session_factory=db_factory,
                     base_url="http://localhost:8080", secrets_key=key)
    consent_url = await flow.start_authorization("fastmail")
    s = parse_qs(urlparse(consent_url).query)["state"][0]
    await flow.handle_callback(state=s, code="abc")

    headers = await flow.current_headers("fastmail")
    assert headers["Authorization"] == "Bearer AT"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: AttributeError.

- [ ] **Step 3: Implement**

Add to `jarvis/oauth/flow.py`:

```python
import logging

_log = logging.getLogger(__name__)


# Inside OAuthFlow:
async def revoke(self, provider_key: str) -> None:
    if self._session_factory is None:
        raise RuntimeError("OAuthFlow needs a session_factory for revoke")
    entry = OAUTH_CATALOG[provider_key]

    async with self._session_factory() as session:
        cred = await OAuthCredentialsRepo(session).get(provider_key)
    if cred is None:
        return  # nothing to revoke

    # Best-effort revocation against provider.
    try:
        metadata = await self.discover(entry)
        if metadata.revocation_endpoint and cred.access_token_enc:
            access_token = decrypt_blob(cred.access_token_enc, self._secrets_key).decode()
            client_id = decrypt_blob(cred.client_id_enc, self._secrets_key).decode()
            client_secret = (
                decrypt_blob(cred.client_secret_enc, self._secrets_key).decode()
                if cred.client_secret_enc else None
            )
            form = {"token": access_token, "token_type_hint": "access_token"}
            headers: dict[str, str] = {}
            if client_secret is not None:
                basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                headers["Authorization"] = f"Basic {basic}"
            else:
                form["client_id"] = client_id
            try:
                await self._http.post(metadata.revocation_endpoint, data=form, headers=headers)
            except httpx.HTTPError as e:
                _log.warning("revocation HTTP error for %s: %s", provider_key, e)
    except Exception:
        _log.exception("revocation pre-step failed for %s; proceeding with local cleanup", provider_key)

    async with self._session_factory() as session:
        await OAuthCredentialsRepo(session).delete(provider_key)


async def current_headers(self, provider_key: str) -> dict[str, str]:
    """Return the current `Authorization: Bearer ...` header for an active provider."""
    if self._session_factory is None:
        raise RuntimeError("OAuthFlow needs a session_factory for current_headers")
    async with self._session_factory() as session:
        cred = await OAuthCredentialsRepo(session).get(provider_key)
    if cred is None or not cred.access_token_enc:
        raise LookupError(f"{provider_key}: no active credentials")
    access_token = decrypt_blob(cred.access_token_enc, self._secrets_key).decode()
    return {"Authorization": f"Bearer {access_token}"}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_flow.py -v
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow.py
git commit -m "add OAuthFlow.revoke and current_headers"
```

---

## Task 18: `MCPManager.replace_oauth_server` and `remove_oauth_server`

**Files:**
- Modify: `jarvis/mcp/manager.py`
- Test: `tests/integration/test_mcp_manager_oauth.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_mcp_manager_oauth.py`:

```python
"""MCPManager OAuth integration: replace, remove, isolation from YAML servers."""

import pytest

from jarvis.config.schema import MCPServersConfig
from jarvis.mcp.manager import MCPManager
from jarvis.persistence.db import Base, create_engine, session_factory


class FakeSDKServer:
    """Duck-typed agents.mcp server used as a fake in tests."""

    def __init__(self, *, list_tools_returns=None, list_tools_raises=None):
        self._list_returns = list_tools_returns or []
        self._list_raises = list_tools_raises
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def list_tools(self):
        if self._list_raises:
            raise self._list_raises
        return self._list_returns


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_replace_oauth_server_swaps_sdk_object(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        first = FakeSDKServer()
        second = FakeSDKServer()
        builds = iter([first, second])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers: next(builds),
        )

        await mgr.replace_oauth_server(
            "fastmail", url="https://api.fastmail.com/mcp",
            headers={"Authorization": "Bearer A1"},
        )
        assert mgr.agent_mcp_servers() == [first]
        assert first.entered

        await mgr.replace_oauth_server(
            "fastmail", url="https://api.fastmail.com/mcp",
            headers={"Authorization": "Bearer A2"},
        )
        assert mgr.agent_mcp_servers() == [second]
        assert second.entered
        # Old one is closed (eventually). Allow event loop to settle.
        import asyncio
        await asyncio.sleep(0)
        assert first.exited
    finally:
        await mgr.stop()


async def test_replace_oauth_server_aborts_on_list_tools_failure(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        first = FakeSDKServer(list_tools_returns=[])
        broken = FakeSDKServer(list_tools_raises=RuntimeError("bad token"))
        builds = iter([first, broken])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers: next(builds),
        )
        await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A1"})
        with pytest.raises(RuntimeError, match="bad token"):
            await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A2"})
        # Old server still active.
        assert mgr.agent_mcp_servers() == [first]
        assert not first.exited
    finally:
        await mgr.stop()


async def test_remove_oauth_server_closes_and_drops(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        sdk = FakeSDKServer()
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers: sdk,
        )
        await mgr.replace_oauth_server("fastmail", url="x", headers={"Authorization": "Bearer A"})
        await mgr.remove_oauth_server("fastmail")
        assert mgr.agent_mcp_servers() == []
        assert sdk.exited
    finally:
        await mgr.stop()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_mcp_manager_oauth.py -v
```

Expected: AttributeError.

- [ ] **Step 3: Implement**

In `jarvis/mcp/manager.py`, add the methods and a small helper that wraps the SDK class so tests can monkeypatch one symbol:

```python
import asyncio
from agents.mcp import MCPServerStreamableHttp


def _build_streamable_http(url: str, headers: dict[str, str]) -> object:
    """Module-level builder so tests can patch this single symbol."""
    return MCPServerStreamableHttp(
        name="oauth",
        params={"url": url, "headers": headers},
    )


# Inside MCPManager:
async def replace_oauth_server(
    self, provider_key: str, *, url: str, headers: dict[str, str]
) -> None:
    new_stack = AsyncExitStack()
    new_sdk = _build_streamable_http(url, headers)
    await new_stack.enter_async_context(new_sdk)

    try:
        tools = await _list_tools(new_sdk)
    except Exception:
        # Botched build — close the new one and leave existing intact.
        await new_stack.aclose()
        raise

    old_stack = self._stacks.get(provider_key)
    self._sdk_servers[provider_key] = new_sdk
    self._stacks[provider_key] = new_stack

    if old_stack is not None:
        # Close on the next event-loop tick so any in-flight call has a moment.
        asyncio.create_task(_aclose_silently(old_stack))

    # Refresh tools rows.
    async with self._session_factory() as session:
        srepo = MCPServerRepo(session)
        trepo = MCPToolRepo(session)
        row = await srepo.upsert(name=provider_key, transport="http")
        await srepo.set_status(row.id, status="connected", last_error=None)
        await trepo.replace_for_server(row.id, tools=tools)


async def remove_oauth_server(self, provider_key: str) -> None:
    sdk = self._sdk_servers.pop(provider_key, None)
    stack = self._stacks.pop(provider_key, None)
    if stack is not None:
        try:
            await stack.aclose()
        except Exception:
            _log.exception("error closing oauth server stack %r", provider_key)


async def _aclose_silently(stack: AsyncExitStack) -> None:
    try:
        await stack.aclose()
    except Exception:
        _log.exception("error closing exit stack")
```

`_aclose_silently` is a module-level coroutine, not a method. Place it next to `_build_streamable_http`.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_mcp_manager_oauth.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/mcp/manager.py tests/integration/test_mcp_manager_oauth.py
git commit -m "add MCPManager.replace_oauth_server and remove_oauth_server"
```

---

## Task 19: `MCPManager` bootstrap iterates the OAuth catalog

**Files:**
- Modify: `jarvis/mcp/manager.py`
- Modify: `tests/integration/test_mcp_manager_oauth.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_mcp_manager_oauth.py`:

```python
from datetime import UTC, datetime, timedelta

from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.store import OAuthCredentialsRepo


async def test_start_iterates_catalog_and_attaches_oauth_server(factory, monkeypatch):
    """When oauth_credentials has a valid Fastmail row, start() builds the SDK server."""
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="fastmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=None,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=[],
        )

    sdk = FakeSDKServer()
    monkeypatch.setattr("jarvis.mcp.manager._build_streamable_http", lambda url, headers: sdk)

    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory, secrets_key=key)
    await mgr.start()
    try:
        assert sdk.entered
        assert mgr.agent_mcp_servers() == [sdk]
    finally:
        await mgr.stop()


async def test_start_skips_oauth_provider_without_credentials(factory, monkeypatch):
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory, secrets_key=generate_key().encode())
    builds = []
    monkeypatch.setattr(
        "jarvis.mcp.manager._build_streamable_http",
        lambda url, headers: builds.append(1),
    )
    await mgr.start()
    try:
        assert builds == []
        assert mgr.agent_mcp_servers() == []
    finally:
        await mgr.stop()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_mcp_manager_oauth.py -v
```

Expected: TypeError on the new `secrets_key` constructor arg.

- [ ] **Step 3: Implement bootstrap iteration**

Edit `jarvis/mcp/manager.py`:

1. Update the constructor to accept `secrets_key: bytes | None = None`. If `None`, OAuth catalog iteration is skipped (preserves the existing one-arg test path; when wired in `main.py` the real key is always passed).
2. After the YAML server loop in `start()`, iterate the catalog:

```python
# At top of file:
from jarvis.oauth.catalog import OAUTH_CATALOG, AuthMode
from jarvis.oauth.crypto import decrypt_blob
from jarvis.oauth.store import OAuthCredentialsRepo

# Constructor:
def __init__(
    self,
    *,
    config: MCPServersConfig,
    session_factory: async_sessionmaker[AsyncSession],
    secrets_key: bytes | None = None,
) -> None:
    self._config = config
    self._session_factory = session_factory
    self._secrets_key = secrets_key
    self._stacks: dict[str, AsyncExitStack] = {}
    self._sdk_servers: dict[str, object] = {}

# In start(), after the YAML loop:
if self._secrets_key is not None:
    await self._bootstrap_oauth_catalog()


async def _bootstrap_oauth_catalog(self) -> None:
    async with self._session_factory() as session:
        repo = OAuthCredentialsRepo(session)
        rows = await repo.list_all()
    rows_by_key = {r.provider_key: r for r in rows}
    for key, entry in OAUTH_CATALOG.items():
        if entry.auth_mode is not AuthMode.DCR:
            continue
        cred = rows_by_key.get(key)
        if cred is None or cred.status != "connected" or not cred.access_token_enc:
            continue
        access_token = decrypt_blob(cred.access_token_enc, self._secrets_key).decode()
        try:
            await self.replace_oauth_server(
                key,
                url=entry.mcp_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except Exception as e:
            _log.exception("failed to attach OAuth MCP %r at boot", key)
            async with self._session_factory() as session:
                await OAuthCredentialsRepo(session).set_status(
                    key, status="needs_reauth", last_error=f"boot attach failed: {e}"
                )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_mcp_manager_oauth.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run the full mcp-manager test file to check no regressions**

```bash
uv run pytest tests/integration/test_mcp_manager.py tests/integration/test_mcp_manager_oauth.py -v
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add jarvis/mcp/manager.py tests/integration/test_mcp_manager_oauth.py
git commit -m "MCPManager bootstrap iterates OAuth catalog"
```

---

## Task 20: Web routes — `/oauth/connect/{provider}`

**Files:**
- Create: `jarvis/web/routes/oauth.py`
- Create: `jarvis/web/templates/oauth_callback.html`
- Modify: `jarvis/web/app.py`
- Test: `tests/integration/test_web_oauth.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_web_oauth.py`:

```python
"""Web routes for OAuth connect/callback/disconnect."""

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.oauth.crypto import generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


class _Ctx:
    """Tiny stand-in for AppContext exposing only what oauth routes need."""
    def __init__(self, session_factory_, oauth_flow):
        self.session_factory = session_factory_
        self.oauth_flow = oauth_flow
        self.mcp_manager = None  # set in tests that need it


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


def make_app(ctx) -> TestClient:
    app = create_app(app_context=ctx)
    return TestClient(app)


def fastmail_metadata():
    return {
        "issuer": "https://api.fastmail.com",
        "authorization_endpoint": "https://api.fastmail.com/oauth/authorize",
        "token_endpoint": "https://api.fastmail.com/oauth/token",
        "registration_endpoint": "https://api.fastmail.com/oauth/register",
        "revocation_endpoint": "https://api.fastmail.com/oauth/revoke",
        "code_challenge_methods_supported": ["S256"],
    }


async def test_connect_returns_302_to_consent_url(factory):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid"})
        return httpx.Response(404)
    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     session_factory=factory, base_url="http://localhost:8080",
                     secrets_key=generate_key().encode())
    ctx = _Ctx(factory, flow)

    client = make_app(ctx)
    r = client.get("/oauth/connect/fastmail", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://api.fastmail.com/oauth/authorize")


async def test_connect_unknown_provider_returns_404(factory):
    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     session_factory=factory, base_url="http://localhost:8080",
                     secrets_key=generate_key().encode())
    ctx = _Ctx(factory, flow)
    client = make_app(ctx)
    r = client.get("/oauth/connect/no-such-provider", follow_redirects=False)
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_web_oauth.py -v
```

Expected: 404 (route not registered).

- [ ] **Step 3: Implement the route + register it**

Create `jarvis/web/routes/oauth.py`:

```python
"""OAuth connect / callback / disconnect routes."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.flow import (
    OAuthCallbackError,
    OAuthDiscoveryError,
)

router = APIRouter(prefix="/oauth")
_log = logging.getLogger(__name__)


@router.get("/connect/{provider}")
async def oauth_connect(provider: str, request: Request):
    if provider not in OAUTH_CATALOG:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    ctx = request.app.state.ctx
    try:
        consent_url = await ctx.oauth_flow.start_authorization(provider)
    except OAuthDiscoveryError as e:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "error", "message": str(e), "provider": provider},
            status_code=502,
        )
    return RedirectResponse(consent_url, status_code=302)
```

Create `jarvis/web/templates/oauth_callback.html`:

```html
{% extends "base.html" %}
{% block content %}
<div class="oauth-callback">
  {% if outcome == "success" %}
    <h2>Connected to {{ provider }}.</h2>
    <p>You can close this tab.</p>
  {% elif outcome == "declined" %}
    <h2>Authorization declined.</h2>
    <p>No data was saved. <a href="/mcp">Back to Jarvis</a>.</p>
  {% else %}
    <h2>Authorization failed.</h2>
    <p>{{ message }}</p>
    <p><a href="/mcp">Back to Jarvis</a></p>
  {% endif %}
</div>
{% endblock %}
```

Register the router in `jarvis/web/app.py`:

```python
from jarvis.web.routes.oauth import router as oauth_router
app.include_router(oauth_router)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_web_oauth.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/web/routes/oauth.py jarvis/web/templates/oauth_callback.html jarvis/web/app.py tests/integration/test_web_oauth.py
git commit -m "add /oauth/connect/{provider} route"
```

---

## Task 21: Web route — `/oauth/callback`

**Files:**
- Modify: `jarvis/web/routes/oauth.py`
- Modify: `tests/integration/test_web_oauth.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_web_oauth.py`:

```python
from urllib.parse import parse_qs, urlparse


class _ManagerStub:
    def __init__(self):
        self.replaced = []
    async def replace_oauth_server(self, key, *, url, headers):
        self.replaced.append((key, url, headers))


async def test_callback_happy_path_renders_success_and_swaps_server(factory):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        return httpx.Response(404)
    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     session_factory=factory, base_url="http://localhost:8080",
                     secrets_key=generate_key().encode())
    ctx = _Ctx(factory, flow)
    ctx.mcp_manager = _ManagerStub()

    client = make_app(ctx)
    r = client.get("/oauth/connect/fastmail", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    r2 = client.get(f"/oauth/callback?state={state}&code=abc")
    assert r2.status_code == 200
    assert "Connected" in r2.text
    assert ctx.mcp_manager.replaced == [
        ("fastmail", "https://api.fastmail.com/mcp", {"Authorization": "Bearer AT"}),
    ]


async def test_callback_unknown_state_renders_error(factory):
    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     session_factory=factory, base_url="http://localhost:8080",
                     secrets_key=generate_key().encode())
    ctx = _Ctx(factory, flow)
    ctx.mcp_manager = _ManagerStub()
    client = make_app(ctx)
    r = client.get("/oauth/callback?state=bogus&code=zzz")
    assert r.status_code == 400
    assert "Authorization failed" in r.text


async def test_callback_with_error_param_renders_declined(factory):
    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     session_factory=factory, base_url="http://localhost:8080",
                     secrets_key=generate_key().encode())
    ctx = _Ctx(factory, flow)
    ctx.mcp_manager = _ManagerStub()
    client = make_app(ctx)
    # state doesn't need to match a real pending row when error is set.
    r = client.get("/oauth/callback?error=access_denied&state=anything")
    assert r.status_code == 200
    assert "declined" in r.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_web_oauth.py -v
```

Expected: AttributeError on `oauth_callback`.

- [ ] **Step 3: Implement**

Add to `jarvis/web/routes/oauth.py`:

```python
@router.get("/callback")
async def oauth_callback(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    qp = request.query_params
    error = qp.get("error")

    if error is not None:
        # Best-effort: sweep any matching pending row but don't fail if absent.
        state = qp.get("state")
        if state:
            try:
                from jarvis.oauth.store import OAuthPendingRepo
                async with ctx.session_factory() as session:
                    await OAuthPendingRepo(session).delete(state)
            except Exception:
                _log.exception("failed to sweep pending row on declined callback")
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "declined", "provider": "", "message": error},
        )

    state = qp.get("state")
    code = qp.get("code")
    if not state or not code:
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "error", "provider": "", "message": "missing state or code"},
            status_code=400,
        )

    try:
        result = await ctx.oauth_flow.handle_callback(state=state, code=code)
    except OAuthCallbackError as e:
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "error", "provider": "", "message": str(e)},
            status_code=400,
        )

    # Attach the SDK server with fresh headers.
    headers = await ctx.oauth_flow.current_headers(result.provider_key)
    entry = OAUTH_CATALOG[result.provider_key]
    if ctx.mcp_manager is not None:
        try:
            await ctx.mcp_manager.replace_oauth_server(
                result.provider_key, url=entry.mcp_url, headers=headers
            )
        except Exception as e:
            _log.exception("post-callback MCP attach failed for %s", result.provider_key)
            return templates.TemplateResponse(
                request,
                "oauth_callback.html",
                {"outcome": "error", "provider": result.provider_key, "message": f"connected, but MCP attach failed: {e}"},
                status_code=500,
            )

    return templates.TemplateResponse(
        request,
        "oauth_callback.html",
        {"outcome": "success", "provider": entry.display_name, "message": ""},
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_web_oauth.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/web/routes/oauth.py tests/integration/test_web_oauth.py
git commit -m "add /oauth/callback route"
```

---

## Task 22: Web route — `POST /oauth/disconnect/{provider}`

**Files:**
- Modify: `jarvis/web/routes/oauth.py`
- Modify: `tests/integration/test_web_oauth.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_web_oauth.py`:

```python
from jarvis.oauth.store import OAuthCredentialsRepo


class _ManagerStubWithRemove(_ManagerStub):
    def __init__(self):
        super().__init__()
        self.removed = []
    async def remove_oauth_server(self, key):
        self.removed.append(key)


async def test_disconnect_revokes_and_removes(factory):
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/register":
            return httpx.Response(201, json={"client_id": "cid", "client_secret": "sec"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        if request.url.path == "/oauth/revoke":
            return httpx.Response(200)
        return httpx.Response(404)

    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     session_factory=factory, base_url="http://localhost:8080",
                     secrets_key=generate_key().encode())
    ctx = _Ctx(factory, flow)
    ctx.mcp_manager = _ManagerStubWithRemove()

    client = make_app(ctx)
    r = client.get("/oauth/connect/fastmail", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get(f"/oauth/callback?state={state}&code=abc")

    r2 = client.post("/oauth/disconnect/fastmail")
    assert r2.status_code in (200, 303)
    assert ctx.mcp_manager.removed == ["fastmail"]
    async with factory() as session:
        assert await OAuthCredentialsRepo(session).get("fastmail") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_web_oauth.py -v
```

Expected: 405 Method Not Allowed.

- [ ] **Step 3: Implement**

Add to `jarvis/web/routes/oauth.py`:

```python
@router.post("/disconnect/{provider}")
async def oauth_disconnect(provider: str, request: Request):
    if provider not in OAUTH_CATALOG:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    ctx = request.app.state.ctx
    if ctx.mcp_manager is not None:
        await ctx.mcp_manager.remove_oauth_server(provider)
    await ctx.oauth_flow.revoke(provider)
    return RedirectResponse("/mcp", status_code=303)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_web_oauth.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/web/routes/oauth.py tests/integration/test_web_oauth.py
git commit -m "add /oauth/disconnect/{provider} route"
```

---

## Task 23: Extend `/mcp` page with OAuth Providers section

**Files:**
- Modify: `jarvis/web/routes/mcp.py`
- Modify: `jarvis/web/templates/mcp.html`
- Test: `tests/integration/test_web_mcp.py` (extend)

- [ ] **Step 1: Look at the current `/mcp` view + template**

```bash
cat jarvis/web/routes/mcp.py jarvis/web/templates/mcp.html
```

- [ ] **Step 2: Write the failing test**

Append to `tests/integration/test_web_mcp.py`:

```python
async def test_mcp_page_lists_oauth_providers_disconnected_by_default(...):
    """Render /mcp with no oauth_credentials: catalog cards show Connect button."""
    # Use the same fixture pattern as adjacent tests in this file. The page
    # should contain "Fastmail" (display_name) and a Connect link
    # for the disconnected state.
    ...
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "Fastmail" in resp.text
    assert 'href="/oauth/connect/fastmail"' in resp.text


async def test_mcp_page_shows_connected_pill_when_credentials_present(...):
    """After upserting a connected credentials row, page renders Connected + Disconnect."""
    # Setup: insert oauth_credentials with status='connected'.
    ...
    resp = client.get("/mcp")
    assert "Connected" in resp.text
    assert "Disconnect" in resp.text


async def test_mcp_page_shows_needs_reauth_banner(...):
    """When status='needs_reauth', show last_error and Reconnect."""
    ...
    resp = client.get("/mcp")
    assert "Re-authorization" in resp.text or "needs_reauth" in resp.text.lower()
```

Fill in the fixture details by mirroring the pattern from existing tests in this file.

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_web_mcp.py -v
```

Expected: assertion failures (Fastmail not present).

- [ ] **Step 4: Update the route**

Edit `jarvis/web/routes/mcp.py`:

```python
"""GET /mcp — MCP server list + tools, plus OAuth Providers section."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.store import OAuthCredentialsRepo
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo

router = APIRouter()


@router.get("/mcp", response_class=HTMLResponse)
async def mcp_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        servers = await MCPServerRepo(session).list_all()
        server_tools = {}
        for srv in servers:
            server_tools[srv.id] = await MCPToolRepo(session).list_for_server(srv.id)
        creds_by_key = {
            r.provider_key: r
            for r in await OAuthCredentialsRepo(session).list_all()
        }

    oauth_cards = []
    for key, entry in OAUTH_CATALOG.items():
        cred = creds_by_key.get(key)
        if cred is None or not cred.access_token_enc:
            state = "disconnected"
        elif cred.status == "needs_reauth":
            state = "needs_reauth"
        else:
            state = "connected"
        oauth_cards.append({
            "key": key,
            "display_name": entry.display_name,
            "state": state,
            "last_error": cred.last_error if cred else None,
            "updated_at": cred.updated_at if cred else None,
        })

    return templates.TemplateResponse(
        request,
        "mcp.html",
        {"servers": servers, "server_tools": server_tools, "oauth_cards": oauth_cards},
    )
```

- [ ] **Step 5: Update the template**

Edit `jarvis/web/templates/mcp.html`. Add a new section above the existing server list:

```html
<section class="oauth-providers">
  <h2>OAuth Providers</h2>
  {% for card in oauth_cards %}
    <div class="oauth-card oauth-card--{{ card.state }}">
      <h3>{{ card.display_name }}</h3>
      {% if card.state == "disconnected" %}
        <a class="btn btn-primary" href="/oauth/connect/{{ card.key }}">Connect</a>
      {% elif card.state == "connected" %}
        <span class="pill pill--connected">Connected</span>
        <span class="muted">Last refreshed: {{ card.updated_at }}</span>
        <form method="post" action="/oauth/disconnect/{{ card.key }}" style="display:inline">
          <button class="btn btn-secondary" type="submit">Disconnect</button>
        </form>
      {% else %}
        <span class="pill pill--warn">Re-authorization required</span>
        <p class="error">{{ card.last_error }}</p>
        <a class="btn btn-primary" href="/oauth/connect/{{ card.key }}">Reconnect</a>
      {% endif %}
    </div>
  {% endfor %}
</section>
```

(Don't worry about CSS classes mapping to real styles; the existing template uses utility-style class names — match its conventions.)

- [ ] **Step 6: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_web_mcp.py -v
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add jarvis/web/routes/mcp.py jarvis/web/templates/mcp.html tests/integration/test_web_mcp.py
git commit -m "extend /mcp page with OAuth Providers section"
```

---

## Task 24: APScheduler jobs — refresh + sweep

**Files:**
- Create: `jarvis/scheduler/oauth_jobs.py`
- Test: `tests/integration/test_oauth_jobs.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_oauth_jobs.py`:

```python
"""APScheduler job functions for OAuth refresh + pending sweep."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.scheduler.oauth_jobs import oauth_pending_sweep, oauth_token_refresh


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


def fastmail_metadata():
    return {
        "issuer": "https://api.fastmail.com",
        "authorization_endpoint": "https://api.fastmail.com/oauth/authorize",
        "token_endpoint": "https://api.fastmail.com/oauth/token",
        "registration_endpoint": "https://api.fastmail.com/oauth/register",
        "revocation_endpoint": None,
        "code_challenge_methods_supported": ["S256"],
    }


class _MgrStub:
    def __init__(self):
        self.replaced = []
        self.removed = []
    async def replace_oauth_server(self, key, *, url, headers):
        self.replaced.append((key, headers))
    async def remove_oauth_server(self, key):
        self.removed.append(key)


async def test_refresh_job_refreshes_due_provider_and_swaps_server(factory):
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="fastmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=encrypt_blob(b"sec", key),
            access_token_enc=encrypt_blob(b"AT-old", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now + timedelta(seconds=30),  # within 90s window
            scopes_granted=[],
        )

    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT-NEW", "refresh_token": "RT2", "expires_in": 3600})
        return httpx.Response(404)

    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     session_factory=factory, base_url="http://localhost:8080", secrets_key=key)
    mgr = _MgrStub()

    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)
    assert mgr.replaced == [("fastmail", {"Authorization": "Bearer AT-NEW"})]


async def test_refresh_job_marks_needs_reauth_on_invalid_grant(factory):
    key = generate_key().encode()
    now = datetime.now(UTC)
    async with factory() as session:
        await OAuthCredentialsRepo(session).upsert(
            provider_key="fastmail",
            client_id_enc=encrypt_blob(b"cid", key),
            client_secret_enc=None,
            access_token_enc=encrypt_blob(b"AT", key),
            refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=now,  # due now
            scopes_granted=[],
        )
    def handler(request):
        if "/.well-known" in request.url.path:
            return httpx.Response(200, json=fastmail_metadata())
        return httpx.Response(400, json={"error": "invalid_grant"})
    flow = OAuthFlow(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     session_factory=factory, base_url="http://localhost:8080", secrets_key=key)
    mgr = _MgrStub()
    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)
    assert mgr.removed == ["fastmail"]
    async with factory() as session:
        cred = await OAuthCredentialsRepo(session).get("fastmail")
        assert cred.status == "needs_reauth"


async def test_pending_sweep_removes_old_rows(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthPendingRepo(session)
        await repo.insert(state="old", provider_key="fastmail", code_verifier="v",
                          now=now - timedelta(hours=2))
        await repo.insert(state="new", provider_key="fastmail", code_verifier="v",
                          now=now - timedelta(seconds=10))
    n = await oauth_pending_sweep(session_factory=factory, ttl_seconds=600)
    assert n == 1
    async with factory() as session:
        assert await OAuthPendingRepo(session).get("old") is None
        assert await OAuthPendingRepo(session).get("new") is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/integration/test_oauth_jobs.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Create `jarvis/scheduler/oauth_jobs.py`:

```python
"""APScheduler job functions for OAuth: proactive refresh and pending sweep."""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.flow import (
    OAuthFlow,
    OAuthRefreshPermanentError,
    OAuthRefreshTransientError,
)
from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo

_log = logging.getLogger(__name__)


async def oauth_token_refresh(
    *,
    flow: OAuthFlow,
    mcp_manager,
    session_factory: async_sessionmaker[AsyncSession],
    skew_seconds: int = 90,
) -> None:
    """Refresh tokens that fall within the skew window. Swap or remove SDK servers."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = await OAuthCredentialsRepo(session).list_due_for_refresh(
            now=now, skew_seconds=skew_seconds
        )

    for cred in due:
        provider_key = cred.provider_key
        entry = OAUTH_CATALOG.get(provider_key)
        if entry is None:
            _log.warning("oauth refresh: unknown provider %r in DB; skipping", provider_key)
            continue
        try:
            new_headers = await flow.refresh(provider_key)
        except OAuthRefreshTransientError as e:
            _log.info("oauth refresh transient failure for %s: %s", provider_key, e)
            continue
        except OAuthRefreshPermanentError as e:
            _log.warning("oauth refresh permanent failure for %s: %s", provider_key, e)
            try:
                await mcp_manager.remove_oauth_server(provider_key)
            except Exception:
                _log.exception("failed to remove SDK server after needs_reauth")
            continue
        try:
            await mcp_manager.replace_oauth_server(
                provider_key, url=entry.mcp_url, headers=new_headers
            )
        except Exception:
            _log.exception("failed to swap SDK server after refresh for %s", provider_key)


async def oauth_pending_sweep(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    ttl_seconds: int = 600,
) -> int:
    """Delete oauth_pending rows older than ttl_seconds. Returns number deleted."""
    async with session_factory() as session:
        return await OAuthPendingRepo(session).sweep_expired(
            now=datetime.now(UTC), ttl_seconds=ttl_seconds
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_oauth_jobs.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add jarvis/scheduler/oauth_jobs.py tests/integration/test_oauth_jobs.py
git commit -m "add OAuth refresh and pending-sweep APScheduler jobs"
```

---

## Task 25: Wire OAuth into bootstrap (`main.py`)

**Files:**
- Modify: `jarvis/main.py`
- Modify: `jarvis/scheduler/scheduler.py` (to register the new jobs)

- [ ] **Step 1: Read current `main.py`**

```bash
cat jarvis/main.py
```

Identify where to inject the `OAuthFlow` and where to register APScheduler jobs.

- [ ] **Step 2: Build the `OAuthFlow` after the DB is up**

In `jarvis/main.py`, inside `bootstrap`, after `audit.start()` but before `mcp_manager.start()`:

```python
import httpx
from jarvis.oauth.flow import OAuthFlow

# ...
oauth_http = httpx.AsyncClient(timeout=30.0)
oauth_flow = OAuthFlow(
    http_client=oauth_http,
    session_factory=factory,
    base_url=cfg.base_url,
    secrets_key=cfg.secrets_key,
)
```

- [ ] **Step 3: Pass `secrets_key` into `MCPManager`**

Update the `MCPManager(...)` constructor call in `bootstrap`:

```python
mcp_manager = MCPManager(
    config=cfg.mcp_servers,
    session_factory=factory,
    secrets_key=cfg.secrets_key,
)
```

- [ ] **Step 4: Add the new fields to `AppContext`**

```python
@dataclass(slots=True)
class AppContext:
    # ... existing fields ...
    oauth_flow: OAuthFlow
    oauth_http: httpx.AsyncClient
```

In `shutdown`, close the http client:

```python
async def shutdown(self) -> None:
    await self.scheduler.stop()
    for adapter in self.channel_adapters:
        try:
            await adapter.stop()
        except Exception:
            _log.exception("error stopping channel adapter")
    await self.mcp_manager.stop()
    await self.oauth_http.aclose()  # NEW
    await self.audit.stop()
    await self.engine.dispose()
```

Pass them in the constructor call at the end of `bootstrap`:

```python
ctx = AppContext(
    # ... existing args ...
    oauth_flow=oauth_flow,
    oauth_http=oauth_http,
)
```

- [ ] **Step 5: Register OAuth APScheduler jobs**

In `jarvis/scheduler/scheduler.py`, add a method or extend `start()` to register the two jobs. The cleanest approach: pass `oauth_flow` and `mcp_manager` into `Scheduler.__init__` and register the jobs in `start()`:

```python
# Scheduler.__init__:
def __init__(self, *, ..., oauth_flow=None, mcp_manager=None, ...):
    # existing
    self._oauth_flow = oauth_flow
    self._oauth_mcp_manager = mcp_manager

# Scheduler.start, after AsyncScheduler is started:
if self._oauth_flow is not None and self._oauth_mcp_manager is not None:
    from jarvis.scheduler.oauth_jobs import oauth_token_refresh, oauth_pending_sweep
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger

    await self._aps.add_schedule(
        oauth_token_refresh,
        trigger=IntervalTrigger(seconds=60),
        kwargs={
            "flow": self._oauth_flow,
            "mcp_manager": self._oauth_mcp_manager,
            "session_factory": self._session_factory,
        },
        id="oauth_token_refresh",
    )
    await self._aps.add_schedule(
        oauth_pending_sweep,
        trigger=CronTrigger(hour=3, minute=0),  # daily 03:00
        kwargs={"session_factory": self._session_factory},
        id="oauth_pending_sweep",
    )
```

If your APScheduler version's API differs (4.0a uses `add_schedule`/`add_job` slightly differently), match the existing call style in `scheduler.py`.

- [ ] **Step 6: Update `main.py` to pass `oauth_flow` and `mcp_manager` into `Scheduler`**

```python
scheduler = Scheduler(
    # ... existing args ...
    oauth_flow=oauth_flow,
    mcp_manager=mcp_manager,
)
```

- [ ] **Step 7: Run the full suite to confirm nothing broke**

```bash
uv run pytest -q
```

Expected: green. If `test_main_smoke.py` fails because `JARVIS_SECRETS_KEY` isn't set, fix the test by `monkeypatch.setenv` of a generated key in the fixture.

- [ ] **Step 8: Commit**

```bash
git add jarvis/main.py jarvis/scheduler/scheduler.py
git commit -m "wire OAuthFlow and OAuth jobs into bootstrap"
```

---

## Task 26: README + manual end-to-end checklist

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a configuration section for OAuth**

Append a new section to `README.md`:

```markdown
## OAuth-protected MCP servers

Jarvis supports OAuth-protected MCP servers (currently: Fastmail) via the dashboard. Setup:

1. **Generate a Fernet secrets key** (one-time):

   ```bash
   uv run python -c "from jarvis.oauth.crypto import generate_key; print(generate_key())"
   ```

2. **Add to your environment** (e.g., `docker-compose.yml` env block):

   ```
   JARVIS_SECRETS_KEY=<paste-the-key>
   JARVIS_BASE_URL=http://localhost:8080   # or https://your-domain for remote deploys
   ```

3. **Restart Jarvis.** Open `http://localhost:8080/mcp` and click **Connect** on the Fastmail card. Complete the consent screen — you'll be redirected back and the card will flip to **Connected**.

4. **Disconnect** at any time using the Disconnect button. This revokes tokens with the provider and deletes local credentials.

> **Key rotation:** changing `JARVIS_SECRETS_KEY` invalidates all stored OAuth credentials. Re-authorize each provider after rotating.
```

- [ ] **Step 2: Add a manual end-to-end checklist for the PR**

Append to the README under a new heading:

```markdown
### Manual end-to-end test (required before merging OAuth changes)

1. Generate a fresh `JARVIS_SECRETS_KEY` and start Jarvis.
2. Navigate to `/mcp`. Click **Connect** on Fastmail.
3. Complete consent on `api.fastmail.com`. Verify redirect to a "Connected to Fastmail" page.
4. Open `/mcp`. Verify the Fastmail card shows **Connected** and tools list under it.
5. Trigger an agent call that uses a Fastmail tool (e.g., via Discord DM). Verify it returns a result.
6. Wait for the access token's `expires_in` to elapse (or manually update `token_expires_at` to a near-past time and wait 60s for the refresh job). Verify the card still shows **Connected** and the tool call still works.
7. Click **Disconnect**. Verify Fastmail card returns to **Connect** state and `oauth_credentials` table is empty.
8. Paste log excerpts and a `/mcp` screenshot into the PR.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "document OAuth MCP setup and manual end-to-end checklist"
```

---

## Final pass: full suite + lint

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest -q
```

Expected: green.

- [ ] **Step 2: Run ruff**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: green. Fix any lints surfaced by the new modules; commit fixes as `style: ruff fixes for OAuth modules` if needed.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin oauth-mcp-management
gh pr create --title "OAuth MCP management (v1: Fastmail)" --body "$(cat <<'EOF'
## Summary
- Adds OAuth-protected MCP server management to the dashboard
- v1 ships Fastmail via Dynamic Client Registration
- Refactors MCPManager to per-server exit stacks for live token rotation

## Test plan
- [ ] All unit + integration tests pass (`uv run pytest`)
- [ ] Manual end-to-end against `https://api.fastmail.com/mcp` (see README checklist)
- [ ] `/mcp` page renders Connect / Connected / Reconnect states correctly
- [ ] Token refresh job runs every 60s; Disconnect revokes and cleans up
EOF
)"
```

---

## Self-review notes (read before executing)

This plan was self-reviewed for spec coverage, placeholders, and type consistency:

- **Spec coverage:** every section of the spec has at least one task. The "open seams" (manual mode, on-401 retry, scope-narrowing UI) are intentionally not implemented; `auth_mode=MANUAL` raises `NotImplementedError` per Task 12.
- **Type consistency:** `MCPManager.replace_oauth_server` is called consistently across Tasks 18, 19, 21, 24, 25 with the same `(provider_key, *, url, headers)` signature.
- **Possible footgun:** Task 14's transient state (credentials row exists with empty `access_token_enc`) is documented inline; bootstrap iteration in Task 19 explicitly skips rows with empty `access_token_enc`. Verify this contract holds end-to-end during execution.
- **APScheduler API:** Task 25 assumes APScheduler 4.0a's `add_schedule` API — the actual call style in the codebase's `scheduler.py` is the source of truth; match it.
- **Test fixtures:** Many tests share a `factory` fixture pattern. If a future refactor moves them to `conftest.py`, do that as part of executing the plan, not as a separate task.

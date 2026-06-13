# MCP Management — Phase 1: Provider/Connection Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded OAuth catalog and the credential-conflating `oauth_credentials` table with a DB-backed provider catalog and per-account connections, rekey the OAuth flow / MCP manager / scheduler around connections, and restrict YAML to stdio — with zero externally-visible behavior change (Gmail/Calendar/Fastmail keep working, each with one `default` connection).

**Architecture:** A **Provider** (`mcp_providers`) is a secret-free service definition; a **Connection** (`mcp_connections`) is one credentialed account instance of a provider. The hardcoded `OAUTH_CATALOG` becomes `SEED_PROVIDERS` used only for seeding; a `ProviderCatalog` reconstructs the existing `ProviderEntry` shape from DB rows at runtime. `OAuthFlow`/manager/scheduler key off `connection_id` / `runtime_name`. `MCPServerRow`/`MCPToolRow` are unchanged in role (live-runtime mirror).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, Fernet (`cryptography`), pytest + pytest-asyncio, httpx `MockTransport`.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `jarvis/persistence/models.py` | ORM rows | Add `MCPProviderRow`, `MCPConnectionRow`; add `connection_id`+`source` to `MCPServerRow`; rename `OAuthPendingRow`→`MCPPendingRow` (col `connection_id`); delete `OAuthCredentialsRow` |
| `jarvis/oauth/catalog.py` | Provider definitions + resolver | Rename `OAUTH_CATALOG`→`SEED_PROVIDERS`; add `default_scopes`/`kind`/`header_names` to `ProviderEntry`; add `ProviderCatalog`, `seed_built_in_providers`, `slug_label`; keep `assert_no_yaml_collision` |
| `jarvis/oauth/store.py` | Repos | Replace `OAuthCredentialsRepo`→`MCPConnectionRepo`; add `MCPProviderRepo`; rekey `OAuthPendingRepo`→`MCPPendingRepo` (connection_id) |
| `jarvis/oauth/flow.py` | OAuth state machine | Rekey every method `provider_key`→`connection_id`; read client creds+scopes from connection, definition from `ProviderCatalog` |
| `jarvis/mcp/manager.py` | Live connection lifecycle | Key live servers by `runtime_name`; connect enabled connections at start; add `connect_connection`/`disconnect`; bootstrap from connections |
| `jarvis/scheduler/oauth_jobs.py` | Proactive refresh | Iterate due **connections**; refresh by connection_id; apply token by runtime_name |
| `jarvis/config/schema.py` | YAML schema | Restrict `MCPServerConfig.transport` to `stdio` |
| `jarvis/web/routes/mcp.py` | `/mcp` page | Build cards from providers+connections (one default each) so the page still renders |
| `jarvis/web/routes/oauth.py` | connect/callback/disconnect | Rekey to connection_id, resolve via `ProviderCatalog` |
| `jarvis/main.py` | Bootstrap | Build `ProviderCatalog`; call `seed_built_in_providers`; pass catalog to flow/manager/scheduler; add to `AppContext` |
| `alembic/versions/0011_provider_connection_model.py` | Schema + data migration | Create new tables; seed providers; convert `oauth_credentials`→connection; import env; drop `oauth_credentials` |

**Behavior-preserving contract:** at the end of Phase 1, `/mcp` shows the same OAuth cards and server list as before, Connect/Disconnect work, and proactive refresh works — now all DB-backed with a single `default` connection per provider.

---

## Task 1: Provider & connection ORM models

**Files:**
- Modify: `jarvis/persistence/models.py` (after `MCPToolRow`, ~line 272; `MCPServerRow` ~242; `OAuthCredentialsRow` ~281; `OAuthPendingRow` ~297)
- Test: `tests/integration/test_mcp_registry_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_mcp_registry_models.py
"""mcp_providers / mcp_connections ORM rows persist and relate."""
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import MCPConnectionRow, MCPProviderRow


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_provider_with_connections_round_trips(factory):
    now = datetime.now(UTC)
    async with factory() as s:
        s.add(MCPProviderRow(
            key="calendar", display_name="Google Calendar", kind="oauth",
            mcp_url="https://calendarmcp.googleapis.com/mcp/v1", builtin=True,
            auth_mode="manual", oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            default_scopes=["a", "b"], created_at=now, updated_at=now,
        ))
        s.add(MCPConnectionRow(
            provider_key="calendar", label="Work", runtime_name="calendar:work",
            scopes=["a"], created_at=now, updated_at=now,
        ))
        await s.commit()

    async with factory() as s:
        prov = (await s.execute(select(MCPProviderRow))).scalar_one()
        conns = (await s.execute(select(MCPConnectionRow))).scalars().all()
        assert prov.key == "calendar"
        assert prov.builtin is True
        assert len(conns) == 1
        assert conns[0].runtime_name == "calendar:work"
        assert conns[0].access_token_enc is None  # not-yet-authorized sentinel
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_registry_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'MCPProviderRow'`.

- [ ] **Step 3: Add the models**

In `jarvis/persistence/models.py`, add after `MCPToolRow`:

```python
class MCPProviderRow(Base):
    """Catalog entry: a secret-free service definition. stdio is NOT represented here."""
    __tablename__ = "mcp_providers"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # 'oauth' | 'http' | 'sse'
    mcp_url: Mapped[str] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(default=False)
    # oauth protocol facts (invariant across accounts)
    auth_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'dcr'|'manual'
    oauth_metadata_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pkce: Mapped[bool] = mapped_column(default=True)
    send_resource_indicator: Mapped[bool] = mapped_column(default=True)
    extra_auth_params: Mapped[dict] = mapped_column(JSON, default=dict)
    # non-authoritative form-prefill hints
    default_scopes: Mapped[list] = mapped_column(JSON, default=list)
    header_names: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    connections: Mapped[list["MCPConnectionRow"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class MCPConnectionRow(Base):
    """One credentialed account instance of a provider -> one live MCP server."""
    __tablename__ = "mcp_connections"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider_key: Mapped[str] = mapped_column(
        ForeignKey("mcp_providers.key", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(128))
    runtime_name: Mapped[str] = mapped_column(String(255), unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    # oauth client credentials (per connection; encrypted)
    client_id_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    client_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # oauth tokens (encrypted). access_token_enc IS NULL == registered but not authorized.
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    scopes_granted: Mapped[list] = mapped_column(JSON, default=list)
    # http/sse
    url_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # oauth credential/auth status (NOT the live-runtime status, which lives on MCPServerRow)
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    provider: Mapped[MCPProviderRow] = relationship(back_populates="connections")
```

Add `connection_id` + `source` to `MCPServerRow` (after `last_connected_at`, ~line 250):

```python
    source: Mapped[str] = mapped_column(String(16), default="stdio")  # 'stdio' | 'connection'
    connection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("mcp_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_mcp_registry_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/models.py tests/integration/test_mcp_registry_models.py
git commit -m "feat(mcp): add provider/connection ORM models"
```

---

## Task 2: `ProviderEntry` fields + `SEED_PROVIDERS` + `slug_label`

**Files:**
- Modify: `jarvis/oauth/catalog.py`
- Test: `tests/unit/test_oauth_catalog.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/test_oauth_catalog.py
from jarvis.oauth.catalog import SEED_PROVIDERS, slug_label


def test_seed_providers_have_kind_and_default_scopes():
    cal = SEED_PROVIDERS["calendar"]
    assert cal.kind == "oauth"
    assert cal.default_scopes  # documented scope set
    assert cal.display_name == "Google Calendar"


def test_slug_label_lowercases_and_dashes():
    assert slug_label("Work Account!") == "work-account"
    assert slug_label("  Personal  ") == "personal"
    assert slug_label("a/b") == "a-b"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_oauth_catalog.py -v`
Expected: FAIL — `ImportError: cannot import name 'SEED_PROVIDERS'`.

- [ ] **Step 3: Edit `catalog.py`**

Add to `ProviderEntry` (after `key`/`display_name`/`mcp_url`): `kind: str = "oauth"`, and rename the existing `scopes` usage by adding `default_scopes: tuple[str, ...] = ()` and `header_names: tuple[str, ...] = ()`. Keep `scopes` removed in favor of `default_scopes` (it was the catalog default scope set). Update the three entries: rename `scopes=` to `default_scopes=`, add `kind="oauth"`. Rename the dict `OAUTH_CATALOG` to `SEED_PROVIDERS`. Add:

```python
import re

def slug_label(label: str) -> str:
    """Stable connection slug: lowercase, non-alphanumeric runs -> single dash."""
    return re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
```

Keep `assert_no_yaml_collision` but update its body to accept the catalog keys argument explicitly (see Task 8). For now leave it referencing `SEED_PROVIDERS`:

```python
def assert_no_yaml_collision(yaml_server_names: Iterable[str], catalog_keys: Iterable[str]) -> None:
    overlap = set(catalog_keys) & set(yaml_server_names)
    if overlap:
        joined = ", ".join(sorted(overlap))
        raise ValueError(
            f"stdio MCP server name(s) collide with provider keys: {joined}. Rename the stdio server(s)."
        )
```

- [ ] **Step 4: Fix existing references that break**

`grep -rn "OAUTH_CATALOG\|\.scopes\b" jarvis tests | grep -i catalog` — update `flow.py`, `manager.py`, `oauth_jobs.py`, `routes/*.py`, and `test_oauth_flow.py` imports in their own tasks below. For THIS task only ensure `catalog.py` and `test_oauth_catalog.py` are consistent; other modules are fixed in their tasks.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/unit/test_oauth_catalog.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/oauth/catalog.py tests/unit/test_oauth_catalog.py
git commit -m "feat(mcp): SEED_PROVIDERS + ProviderEntry kind/default_scopes/header_names + slug_label"
```

---

## Task 3: `MCPProviderRepo` + `ProviderCatalog` + `seed_built_in_providers`

**Files:**
- Modify: `jarvis/oauth/store.py` (add `MCPProviderRepo`)
- Modify: `jarvis/oauth/catalog.py` (add `ProviderCatalog`, `seed_built_in_providers`)
- Test: `tests/integration/test_provider_catalog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_provider_catalog.py
import pytest_asyncio

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_seed_then_catalog_reconstructs_provider_entry(factory):
    async with factory() as s:
        await seed_built_in_providers(s)
    catalog = ProviderCatalog(factory)
    cal = await catalog.get("calendar")
    assert cal.kind == "oauth"
    assert cal.auth_mode.value == "manual"
    assert cal.mcp_url.endswith("/mcp/v1")
    keys = {e.key for e in await catalog.list()}
    assert {"gmail", "calendar", "fastmail"} <= keys


async def test_seed_is_idempotent(factory):
    async with factory() as s:
        await seed_built_in_providers(s)
    async with factory() as s:
        await seed_built_in_providers(s)  # second run must not duplicate or raise
    catalog = ProviderCatalog(factory)
    assert len([e for e in await catalog.list() if e.key == "gmail"]) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_provider_catalog.py -v`
Expected: FAIL — `ImportError: cannot import name 'ProviderCatalog'`.

- [ ] **Step 3: Add `MCPProviderRepo` to `store.py`**

```python
from jarvis.persistence.models import MCPProviderRow

class MCPProviderRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> MCPProviderRow | None:
        res = await self._session.execute(
            select(MCPProviderRow).where(MCPProviderRow.key == key)
        )
        return res.scalar_one_or_none()

    async def list_all(self) -> list[MCPProviderRow]:
        res = await self._session.execute(select(MCPProviderRow))
        return list(res.scalars())

    async def upsert(self, *, key: str, display_name: str, kind: str, mcp_url: str,
                     builtin: bool, auth_mode: str | None, oauth_metadata_url: str | None,
                     pkce: bool, send_resource_indicator: bool, extra_auth_params: dict,
                     default_scopes: list[str], header_names: list[str]) -> MCPProviderRow:
        now = _utcnow()
        row = await self.get(key)
        if row is None:
            row = MCPProviderRow(key=key, created_at=now)
            self._session.add(row)
        row.display_name = display_name; row.kind = kind; row.mcp_url = mcp_url
        row.builtin = builtin; row.auth_mode = auth_mode
        row.oauth_metadata_url = oauth_metadata_url; row.pkce = pkce
        row.send_resource_indicator = send_resource_indicator
        row.extra_auth_params = extra_auth_params; row.default_scopes = default_scopes
        row.header_names = header_names; row.updated_at = now
        await self._session.commit()
        return row

    async def delete(self, key: str) -> None:
        await self._session.execute(delete(MCPProviderRow).where(MCPProviderRow.key == key))
        await self._session.commit()
```

- [ ] **Step 4: Add `ProviderCatalog` + `seed_built_in_providers` to `catalog.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

class ProviderCatalog:
    """Runtime, DB-backed view of the provider catalog. Reconstructs ProviderEntry rows."""
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    @staticmethod
    def _to_entry(row) -> ProviderEntry:
        return ProviderEntry(
            key=row.key, display_name=row.display_name, kind=row.kind, mcp_url=row.mcp_url,
            auth_mode=AuthMode(row.auth_mode) if row.auth_mode else AuthMode.DCR,
            oauth_metadata_url=row.oauth_metadata_url, pkce=row.pkce,
            send_resource_indicator=row.send_resource_indicator,
            extra_auth_params=dict(row.extra_auth_params or {}),
            default_scopes=tuple(row.default_scopes or ()),
            header_names=tuple(row.header_names or ()),
        )

    async def get(self, key: str) -> ProviderEntry:
        from jarvis.oauth.store import MCPProviderRepo
        async with self._factory() as s:
            row = await MCPProviderRepo(s).get(key)
        if row is None:
            raise KeyError(key)
        return self._to_entry(row)

    async def list(self) -> list[ProviderEntry]:
        from jarvis.oauth.store import MCPProviderRepo
        async with self._factory() as s:
            rows = await MCPProviderRepo(s).list_all()
        return [self._to_entry(r) for r in rows]


async def seed_built_in_providers(session) -> None:
    """Idempotently upsert SEED_PROVIDERS as builtin rows. Definition-only, no secrets."""
    from jarvis.oauth.store import MCPProviderRepo
    repo = MCPProviderRepo(session)
    for entry in SEED_PROVIDERS.values():
        await repo.upsert(
            key=entry.key, display_name=entry.display_name, kind=entry.kind,
            mcp_url=entry.mcp_url, builtin=True,
            auth_mode=entry.auth_mode.value, oauth_metadata_url=entry.oauth_metadata_url,
            pkce=entry.pkce, send_resource_indicator=entry.send_resource_indicator,
            extra_auth_params=dict(entry.extra_auth_params),
            default_scopes=list(entry.default_scopes), header_names=list(entry.header_names),
        )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/integration/test_provider_catalog.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/oauth/store.py jarvis/oauth/catalog.py tests/integration/test_provider_catalog.py
git commit -m "feat(mcp): MCPProviderRepo + DB-backed ProviderCatalog + seed_built_in_providers"
```

---

## Task 4: `MCPConnectionRepo`

**Files:**
- Modify: `jarvis/oauth/store.py` (replace `OAuthCredentialsRepo` with `MCPConnectionRepo`)
- Test: `tests/integration/test_mcp_connection_repo.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_mcp_connection_repo.py
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_create_get_and_set_tokens(factory):
    async with factory() as s:
        conn = await MCPConnectionRepo(s).create(
            provider_key="calendar", label="Work", runtime_name="calendar:work",
            client_id_enc=b"cid", client_secret_enc=b"sec", scopes=["a"],
        )
        cid = conn.id
    async with factory() as s:
        await MCPConnectionRepo(s).set_tokens(
            cid, access_token_enc=b"tok", refresh_token_enc=b"ref",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1), scopes_granted=["a"],
        )
    async with factory() as s:
        got = await MCPConnectionRepo(s).get(cid)
        assert got.access_token_enc == b"tok"
        assert got.status == "connected"


async def test_list_due_for_refresh_filters_by_status_and_expiry(factory):
    soon = datetime.now(UTC) + timedelta(seconds=30)
    async with factory() as s:
        repo = MCPConnectionRepo(s)
        c = await repo.create(provider_key="calendar", label="W", runtime_name="calendar:w")
        await repo.set_tokens(c.id, access_token_enc=b"t", refresh_token_enc=b"r",
                              token_expires_at=soon, scopes_granted=[])
    async with factory() as s:
        due = await MCPConnectionRepo(s).list_due_for_refresh(now=datetime.now(UTC), skew_seconds=90)
        assert [d.runtime_name for d in due] == ["calendar:w"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_connection_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'MCPConnectionRepo'`.

- [ ] **Step 3: Replace `OAuthCredentialsRepo` with `MCPConnectionRepo` in `store.py`**

```python
from uuid import UUID
from jarvis.persistence.models import MCPConnectionRow

class MCPConnectionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, connection_id: UUID) -> MCPConnectionRow | None:
        return await self._session.get(MCPConnectionRow, connection_id)

    async def get_by_runtime_name(self, runtime_name: str) -> MCPConnectionRow | None:
        res = await self._session.execute(
            select(MCPConnectionRow).where(MCPConnectionRow.runtime_name == runtime_name)
        )
        return res.scalar_one_or_none()

    async def list_all(self) -> list[MCPConnectionRow]:
        res = await self._session.execute(select(MCPConnectionRow))
        return list(res.scalars())

    async def list_for_provider(self, provider_key: str) -> list[MCPConnectionRow]:
        res = await self._session.execute(
            select(MCPConnectionRow).where(MCPConnectionRow.provider_key == provider_key)
        )
        return list(res.scalars())

    async def list_enabled(self) -> list[MCPConnectionRow]:
        res = await self._session.execute(
            select(MCPConnectionRow).where(MCPConnectionRow.enabled.is_(True))
        )
        return list(res.scalars())

    async def create(self, *, provider_key: str, label: str, runtime_name: str,
                     client_id_enc: bytes | None = None, client_secret_enc: bytes | None = None,
                     scopes: list[str] | None = None, url_override: str | None = None,
                     headers_enc: bytes | None = None, enabled: bool = True) -> MCPConnectionRow:
        now = _utcnow()
        row = MCPConnectionRow(
            provider_key=provider_key, label=label, runtime_name=runtime_name, enabled=enabled,
            client_id_enc=client_id_enc, client_secret_enc=client_secret_enc,
            scopes=scopes or [], url_override=url_override, headers_enc=headers_enc,
            status="disconnected", created_at=now, updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def set_client(self, connection_id: UUID, *, client_id_enc: bytes,
                         client_secret_enc: bytes | None) -> None:
        row = await self.get(connection_id)
        if row is None:
            raise LookupError(connection_id)
        row.client_id_enc = client_id_enc
        row.client_secret_enc = client_secret_enc
        row.updated_at = _utcnow()
        await self._session.commit()

    async def set_tokens(self, connection_id: UUID, *, access_token_enc: bytes,
                         refresh_token_enc: bytes | None, token_expires_at: datetime,
                         scopes_granted: list[str]) -> None:
        row = await self.get(connection_id)
        if row is None:
            raise LookupError(connection_id)
        row.access_token_enc = access_token_enc
        if refresh_token_enc is not None:
            row.refresh_token_enc = refresh_token_enc
        row.token_expires_at = token_expires_at
        row.scopes_granted = scopes_granted
        row.status = "connected"
        row.last_error = None
        row.connected_at = row.connected_at or _utcnow()
        row.updated_at = _utcnow()
        await self._session.commit()

    async def update_tokens(self, connection_id: UUID, *, access_token_enc: bytes,
                            refresh_token_enc: bytes | None, token_expires_at: datetime) -> None:
        row = await self.get(connection_id)
        if row is None:
            raise LookupError(connection_id)
        row.access_token_enc = access_token_enc
        if refresh_token_enc is not None:
            row.refresh_token_enc = refresh_token_enc
        row.token_expires_at = token_expires_at
        row.status = "connected"
        row.last_error = None
        row.updated_at = _utcnow()
        await self._session.commit()

    async def set_status(self, connection_id: UUID, *, status: str, last_error: str | None) -> None:
        row = await self.get(connection_id)
        if row is None:
            return
        row.status = status
        row.last_error = last_error
        row.updated_at = _utcnow()
        await self._session.commit()

    async def set_enabled(self, connection_id: UUID, *, enabled: bool) -> None:
        row = await self.get(connection_id)
        if row is None:
            return
        row.enabled = enabled
        row.updated_at = _utcnow()
        await self._session.commit()

    async def clear_tokens(self, connection_id: UUID) -> None:
        """Disconnect: drop tokens, keep client + scopes so reconnect is one click."""
        row = await self.get(connection_id)
        if row is None:
            return
        row.access_token_enc = None
        row.refresh_token_enc = None
        row.token_expires_at = None
        row.scopes_granted = []
        row.status = "disconnected"
        row.last_error = None
        row.updated_at = _utcnow()
        await self._session.commit()

    async def delete(self, connection_id: UUID) -> None:
        await self._session.execute(
            delete(MCPConnectionRow).where(MCPConnectionRow.id == connection_id)
        )
        await self._session.commit()

    async def list_due_for_refresh(self, *, now: datetime, skew_seconds: int = 90
                                   ) -> list[MCPConnectionRow]:
        threshold = now + timedelta(seconds=skew_seconds)
        res = await self._session.execute(
            select(MCPConnectionRow).where(
                MCPConnectionRow.status == "connected",
                MCPConnectionRow.token_expires_at.is_not(None),
                MCPConnectionRow.token_expires_at <= threshold,
            )
        )
        return list(res.scalars())
```

Delete the old `OAuthCredentialsRow` import and class usages from `store.py`. Update the module docstring to `"""Repositories for mcp_providers, mcp_connections, and mcp_pending tables."""`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_mcp_connection_repo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/oauth/store.py tests/integration/test_mcp_connection_repo.py
git commit -m "feat(mcp): MCPConnectionRepo (per-account credentials + tokens + status)"
```

---

## Task 5: Rekey `MCPPendingRow` / `MCPPendingRepo` to `connection_id`

**Files:**
- Modify: `jarvis/persistence/models.py` (`OAuthPendingRow`→`MCPPendingRow`, table `mcp_pending`, col `connection_id`)
- Modify: `jarvis/oauth/store.py` (`OAuthPendingRepo`→`MCPPendingRepo`)
- Test: `tests/integration/test_mcp_pending_repo.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_mcp_pending_repo.py
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio

from jarvis.oauth.store import MCPPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_pending_keyed_by_connection_id(factory):
    cid = uuid4()
    async with factory() as s:
        await MCPPendingRepo(s).insert(state="st", connection_id=cid,
                                       code_verifier="v", now=datetime.now(UTC))
    async with factory() as s:
        row = await MCPPendingRepo(s).get("st")
        assert row.connection_id == cid
        assert row.code_verifier == "v"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_pending_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'MCPPendingRepo'`.

- [ ] **Step 3: Rename model and repo**

In `models.py` replace `OAuthPendingRow` with:

```python
class MCPPendingRow(Base):
    __tablename__ = "mcp_pending"
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_connections.id", ondelete="CASCADE"), index=True
    )
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
```

In `store.py` rename `OAuthPendingRepo`→`MCPPendingRepo`, swap `OAuthPendingRow`→`MCPPendingRow`, and change `insert(..., provider_key=...)` to `insert(..., connection_id: UUID, ...)` setting `connection_id=connection_id`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_mcp_pending_repo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/persistence/models.py jarvis/oauth/store.py tests/integration/test_mcp_pending_repo.py
git commit -m "feat(mcp): rekey pending OAuth state to connection_id (mcp_pending)"
```

---

## Task 6: Rekey `OAuthFlow` to connections

**Files:**
- Modify: `jarvis/oauth/flow.py`
- Modify: `tests/integration/test_oauth_flow.py` (update existing tests to new signatures)
- Test: add `tests/integration/test_oauth_flow_connections.py` (multi-connection isolation)

The flow takes a `ProviderCatalog` and operates on `connection_id`. Client creds + scopes come from the **connection**; definition from the catalog.

- [ ] **Step 1: Write the failing isolation test**

```python
# tests/integration/test_oauth_flow_connections.py
"""Two connections on one provider authorize independently."""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest_asyncio

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import decrypt_blob, generate_key
from jarvis.oauth.flow import OAuthFlow
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.persistence.db import Base, create_engine, session_factory

GOOGLE_META = {
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
    "code_challenge_methods_supported": ["S256"],
}


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_two_calendar_connections_authorize_with_their_own_client(factory):
    key = generate_key().encode()

    def handler(request):
        if request.url.path.endswith(".well-known/openid-configuration"):
            return httpx.Response(200, json=GOOGLE_META)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT",
                                             "expires_in": 3600, "scope": "a"})
        return httpx.Response(404)

    flow = OAuthFlow(http_client=make_client(handler), session_factory=factory,
                     base_url="http://localhost:8080", secrets_key=key,
                     catalog=ProviderCatalog(factory))

    # Two connections, distinct manual client_ids.
    async with factory() as s:
        repo = MCPConnectionRepo(s)
        from jarvis.oauth.crypto import encrypt_blob
        work = await repo.create(provider_key="calendar", label="Work",
                                 runtime_name="calendar:work",
                                 client_id_enc=encrypt_blob(b"work-cid", key),
                                 client_secret_enc=encrypt_blob(b"work-sec", key),
                                 scopes=["a"])
        home = await repo.create(provider_key="calendar", label="Home",
                                 runtime_name="calendar:home",
                                 client_id_enc=encrypt_blob(b"home-cid", key),
                                 client_secret_enc=encrypt_blob(b"home-sec", key),
                                 scopes=["a", "b"])

    url_work = await flow.start_authorization(work.id)
    q_work = parse_qs(urlparse(url_work).query)
    assert q_work["client_id"] == ["work-cid"]
    assert q_work["scope"] == ["a"]

    url_home = await flow.start_authorization(home.id)
    q_home = parse_qs(urlparse(url_home).query)
    assert q_home["client_id"] == ["home-cid"]
    assert q_home["scope"] == ["a b"]
    # state rows must differ
    assert q_work["state"] != q_home["state"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_oauth_flow_connections.py -v`
Expected: FAIL — `OAuthFlow.__init__() got an unexpected keyword argument 'catalog'`.

- [ ] **Step 3: Rewrite `flow.py` signatures**

Key changes (apply throughout):
- `__init__` gains `catalog: ProviderCatalog` (store as `self._catalog`).
- `discover` keeps taking a `ProviderEntry`.
- `start_authorization(self, connection_id: UUID) -> str`:
  - load connection via `MCPConnectionRepo.get`; `entry = await self._catalog.get(conn.provider_key)`.
  - client creds: if `conn.client_id_enc` is None → for DCR, `register_client` then `MCPConnectionRepo.set_client(...)`; for MANUAL, raise `OAuthDiscoveryError` (creds must be supplied on the connection — no env fallback). Else decrypt `conn.client_id_enc`.
  - effective scopes: `list(conn.scopes) if conn.scopes else metadata.scopes_supported`.
  - PKCE/state; `MCPPendingRepo.insert(state=..., connection_id=connection_id, ...)`.
  - build params exactly as before (resource indicator from `entry.mcp_url`).
- `handle_callback(self, *, state, code) -> CallbackResult`:
  - `pending = MCPPendingRepo.get(state)`; `connection_id = pending.connection_id`.
  - load conn; `entry = await self._catalog.get(conn.provider_key)`.
  - client_id/secret from `conn.client_id_enc`/`conn.client_secret_enc`.
  - exchange; `MCPConnectionRepo.set_tokens(connection_id, ...)`; delete pending.
  - `CallbackResult` gains `connection_id` + keeps `provider_key` + `runtime_name`.
- `refresh(self, connection_id) -> dict[str,str]`, `revoke(self, connection_id)`, `current_headers(self, connection_id)`, `_mark_needs_reauth(self, connection_id, reason)` — all load the connection, resolve entry via catalog, read creds/tokens from the connection, write via `MCPConnectionRepo`.

`CallbackResult` dataclass becomes:

```python
@dataclass(frozen=True, slots=True)
class CallbackResult:
    connection_id: UUID
    provider_key: str
    runtime_name: str
    scopes_granted: list[str]
```

Delete `_resolve_manual_client` (env-var sourcing) entirely; MANUAL creds now live on the connection. Replace its single call site in `start_authorization` with the connection-creds branch above.

- [ ] **Step 4: Update existing `test_oauth_flow.py`**

The `discover`/`register_client` tests take a `ProviderEntry` directly — replace `OAUTH_CATALOG["fastmail"]` with `SEED_PROVIDERS["fastmail"]` (import rename). They don't need a catalog. The flow-construction lines that pass `session_factory=None` keep working for discover-only tests; add `catalog=None` is not needed because discover/register don't touch `self._catalog`. Construct those flows with `catalog=ProviderCatalog(...)` only where a DB is used. Verify by running the file.

- [ ] **Step 5: Run to verify both pass**

Run: `uv run pytest tests/integration/test_oauth_flow_connections.py tests/integration/test_oauth_flow.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/oauth/flow.py tests/integration/test_oauth_flow_connections.py tests/integration/test_oauth_flow.py
git commit -m "feat(mcp): rekey OAuthFlow to connection_id; creds+scopes from connection"
```

---

## Task 7: Rekey `MCPManager` to `runtime_name` + connection startup

**Files:**
- Modify: `jarvis/mcp/manager.py`
- Modify: `tests/integration/test_mcp_manager_oauth.py`, `tests/integration/test_mcp_manager*.py` (update to new API)
- Test: `tests/integration/test_mcp_manager_connections.py`

The manager's internal dicts are already keyed by a string `name`. We change *what* fills them: connections key by `runtime_name`, and the bootstrap reads connections instead of `OAUTH_CATALOG`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_mcp_manager_connections.py
"""Manager attaches enabled OAuth connections at start, keyed by runtime_name."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest_asyncio

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.config.schema import MCPServersConfig
from jarvis.mcp.manager import MCPManager
from jarvis.persistence.db import Base, create_engine, session_factory


class _FakeSDK:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def list_tools(self): return [MCPToolDescriptor(name="list_events", input_schema={})]


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_enabled_connection_attaches_at_start(factory):
    key = generate_key().encode()
    async with factory() as s:
        await MCPConnectionRepo(s).create(
            provider_key="calendar", label="Work", runtime_name="calendar:work",
            scopes=["a"],
        )
    async with factory() as s:
        conn = await MCPConnectionRepo(s).get_by_runtime_name("calendar:work")
        await MCPConnectionRepo(s).set_tokens(
            conn.id, access_token_enc=encrypt_blob(b"AT", key), refresh_token_enc=encrypt_blob(b"RT", key),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1), scopes_granted=["a"])

    mgr = MCPManager(config=MCPServersConfig(servers=[]), session_factory=factory,
                     secrets_key=key, oauth_flow=None, catalog=ProviderCatalog(factory))
    with patch("jarvis.mcp.manager._build_streamable_http", return_value=_FakeSDK()):
        await mgr.start()
    try:
        assert "calendar:work" in mgr.agent_mcp_context()
    finally:
        await mgr.stop()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_manager_connections.py -v`
Expected: FAIL — `MCPManager.__init__() got an unexpected keyword argument 'catalog'`.

- [ ] **Step 3: Edit `manager.py`**

- `__init__` gains `catalog: ProviderCatalog | None = None` → `self._catalog`.
- In `start()`: keep stdio YAML connect loop, then replace `_bootstrap_oauth_catalog()` with `_bootstrap_connections()`:

```python
async def _bootstrap_connections(self) -> None:
    """Attach every enabled connection at startup, keyed by runtime_name."""
    async with self._session_factory() as session:
        conns = await MCPConnectionRepo(session).list_enabled()
    for conn in conns:
        entry = await self._catalog.get(conn.provider_key)
        try:
            if entry.kind == "oauth":
                if conn.status != "connected" or not conn.access_token_enc:
                    continue  # not authorized yet / needs reauth
                token = decrypt_blob(conn.access_token_enc, self._secrets_key).decode()
                await self.replace_oauth_server(
                    conn.runtime_name, url=entry.mcp_url,
                    headers={"Authorization": f"Bearer {token}"})
            else:  # http / sse
                headers = _decrypt_headers(conn.headers_enc, self._secrets_key)
                url = conn.url_override or entry.mcp_url
                await self.replace_oauth_server(conn.runtime_name, url=url, headers=headers)
        except Exception as e:
            _log.exception("failed to attach connection %r at boot", conn.runtime_name)
            async with self._session_factory() as session:
                await MCPConnectionRepo(session).set_status(
                    conn.id, status="needs_reauth", last_error=f"boot attach failed: {e}")
```

  Call it from `start()` where `_bootstrap_oauth_catalog()` was called (still gated on `self._secrets_key is not None`).
- `refresh_oauth_server_for_retry(self, runtime_name)`: resolve the connection by runtime_name, then `entry = await self._catalog.get(conn.provider_key)`; replace `OAUTH_CATALOG[provider_key]` (line ~203) with that. Pass `conn.id` to `self._oauth_flow.refresh`.
- The `replace`/`remove`/`update_oauth_token`/`_do_replace_oauth`/`_do_remove_oauth` paths already operate on a string key — they now receive `runtime_name`. The `_do_replace_oauth` DB write currently does `MCPServerRepo.upsert(name=provider_key, transport="http")`; change to also set `source="connection"` and link `connection_id` (resolve by runtime_name). Add an optional `connection_id` to `MCPServerRepo.upsert` (default None) and set `source`:

```python
async def upsert(self, *, name: str, transport: str, source: str = "stdio",
                 connection_id: "UUID | None" = None) -> MCPServerRow:
    ...
    if existing:
        existing.transport = transport; existing.source = source
        existing.connection_id = connection_id
        ...
    row = MCPServerRow(name=name, transport=transport, status="disconnected",
                       source=source, connection_id=connection_id)
```

  In `_do_replace_oauth`, resolve the connection id by runtime_name and pass `source="connection", connection_id=...`. (Stdio path in `_do_connect_one` keeps `source="stdio"`.)
- Add the helper and public methods:

```python
def _decrypt_headers(blob: bytes | None, key: bytes) -> dict[str, str]:
    if not blob:
        return {}
    import json
    return json.loads(decrypt_blob(blob, key).decode())

# in MCPManager:
async def connect_connection(self, conn) -> None:
    """Attach one connection (used by dashboard enable/add). Resolves transport from provider."""
    entry = await self._catalog.get(conn.provider_key)
    if entry.kind == "oauth":
        if not conn.access_token_enc:
            return
        token = decrypt_blob(conn.access_token_enc, self._secrets_key).decode()
        await self.replace_oauth_server(conn.runtime_name, url=entry.mcp_url,
                                        headers={"Authorization": f"Bearer {token}"})
    else:
        headers = _decrypt_headers(conn.headers_enc, self._secrets_key)
        await self.replace_oauth_server(conn.runtime_name,
                                        url=conn.url_override or entry.mcp_url, headers=headers)

async def disconnect(self, runtime_name: str) -> None:
    await self.remove_oauth_server(runtime_name)
```

- Update imports: remove `from jarvis.oauth.catalog import OAUTH_CATALOG`; import `MCPConnectionRepo`. Update `assert_no_yaml_collision` call in `start()` to pass catalog keys (Task 8 finalizes the signature; here pass `()` if catalog is None, else the provider keys + runtime names — see Task 8).

- [ ] **Step 4: Update existing manager tests**

`test_mcp_manager_oauth.py` constructs `MCPManager(...)` and seeds `oauth_credentials`; rewrite its setup to seed a provider (`seed_built_in_providers`) + a connection with tokens, pass `catalog=ProviderCatalog(factory)`, and assert on `runtime_name` keys. Run the file and fix references.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/integration/test_mcp_manager_connections.py tests/integration/test_mcp_manager_oauth.py tests/integration/test_mcp_manager.py tests/integration/test_mcp_manager_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/mcp/manager.py jarvis/persistence/repositories.py tests/integration/test_mcp_manager_connections.py tests/integration/test_mcp_manager_oauth.py
git commit -m "feat(mcp): manager attaches connections by runtime_name; catalog-resolved transport"
```

---

## Task 8: Restrict YAML schema to stdio; update collision check

**Files:**
- Modify: `jarvis/config/schema.py`
- Modify: `jarvis/mcp/manager.py` (`start()` collision call)
- Test: `tests/unit/test_config_schema_stdio.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config_schema_stdio.py
import pytest

from jarvis.config.schema import MCPServersConfig


def test_stdio_server_is_accepted():
    cfg = MCPServersConfig.model_validate(
        {"servers": [{"name": "fs", "transport": "stdio", "command": ["npx", "x"]}]}
    )
    assert cfg.servers[0].transport == "stdio"


def test_http_server_in_yaml_is_rejected():
    with pytest.raises(Exception) as ei:
        MCPServersConfig.model_validate(
            {"servers": [{"name": "r", "transport": "http", "url": "http://x"}]}
        )
    assert "stdio" in str(ei.value)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_config_schema_stdio.py -v`
Expected: FAIL — the http server validates today.

- [ ] **Step 3: Edit `schema.py`**

Change `MCPServerConfig` to stdio-only:

```python
class MCPServerConfig(_StrictModel):
    name: str
    transport: Literal["stdio"] = "stdio"
    enabled: bool = True
    command: list[str] | None = None
    env: dict[str, str] | None = None

    @model_validator(mode="after")
    def _stdio_requires_command(self) -> "MCPServerConfig":
        if self.transport != "stdio":
            raise ValueError(
                "mcp-servers.yaml only supports stdio servers; add http/sse servers via the dashboard"
            )
        if not self.command:
            raise ValueError("stdio transport requires `command`")
        return self
```

(Remove `url`/`headers` fields.) Note: with `_StrictModel` (`extra="forbid"`), a YAML `url:` key now errors as an unexpected field, and an explicit `transport: http` errors via the validator — both give clear messages.

- [ ] **Step 4: Finalize `assert_no_yaml_collision` call in `manager.start()`**

```python
async def _collision_keys(self) -> set[str]:
    async with self._session_factory() as s:
        provs = await MCPProviderRepo(s).list_all()
        conns = await MCPConnectionRepo(s).list_all()
    return {p.key for p in provs} | {c.runtime_name for c in conns}

# in start():
if self._catalog is not None:
    assert_no_yaml_collision((s.name for s in self._config.servers), await self._collision_keys())
```

(Import `MCPProviderRepo`.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/unit/test_config_schema_stdio.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/config/schema.py jarvis/mcp/manager.py tests/unit/test_config_schema_stdio.py
git commit -m "feat(config): restrict mcp-servers.yaml to stdio; collision check vs providers/connections"
```

---

## Task 9: Rekey scheduler refresh job to connections

**Files:**
- Modify: `jarvis/scheduler/oauth_jobs.py`
- Modify: `tests/integration/test_oauth_jobs.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/integration/test_oauth_jobs.py (new style)
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio

from jarvis.oauth.catalog import seed_built_in_providers
from jarvis.oauth.crypto import encrypt_blob, generate_key
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.scheduler.oauth_jobs import oauth_token_refresh


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_refresh_due_connection_updates_token_in_place(factory):
    key = generate_key().encode()
    async with factory() as s:
        c = await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                              runtime_name="calendar:w")
        await MCPConnectionRepo(s).set_tokens(
            c.id, access_token_enc=encrypt_blob(b"old", key), refresh_token_enc=encrypt_blob(b"r", key),
            token_expires_at=datetime.now(UTC) + timedelta(seconds=30), scopes_granted=[])

    flow = MagicMock()
    flow.refresh = AsyncMock(return_value={"Authorization": "Bearer NEW"})
    mgr = MagicMock()
    mgr.update_oauth_token = MagicMock(return_value=True)  # live holder present

    await oauth_token_refresh(flow=flow, mcp_manager=mgr, session_factory=factory)

    flow.refresh.assert_awaited_once_with(c.id)
    mgr.update_oauth_token.assert_called_once_with("calendar:w", "NEW")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_oauth_jobs.py::test_refresh_due_connection_updates_token_in_place -v`
Expected: FAIL — job still queries `OAuthCredentialsRepo`.

- [ ] **Step 3: Rewrite `oauth_jobs.py`**

```python
from jarvis.oauth.store import MCPConnectionRepo, MCPPendingRepo

async def oauth_token_refresh(*, flow, mcp_manager, session_factory, catalog=None,
                              skew_seconds: int = 90) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        due = await MCPConnectionRepo(session).list_due_for_refresh(now=now, skew_seconds=skew_seconds)
    for conn in due:
        try:
            new_headers = await flow.refresh(conn.id)
        except OAuthRefreshTransientError as e:
            _log.info("oauth refresh transient failure for %s: %s", conn.runtime_name, e); continue
        except OAuthRefreshPermanentError as e:
            _log.warning("oauth refresh permanent failure for %s: %s", conn.runtime_name, e)
            try:
                await mcp_manager.remove_oauth_server(conn.runtime_name)
            except Exception:
                _log.exception("failed to remove SDK server after needs_reauth")
            continue
        token = new_headers["Authorization"].removeprefix("Bearer ")
        if mcp_manager.update_oauth_token(conn.runtime_name, token):
            continue
        # Not attached yet — full attach (need the provider's url).
        try:
            await asyncio.wait_for(
                mcp_manager.replace_oauth_server(conn.runtime_name,
                    url=conn.url_override or (await mcp_manager._catalog.get(conn.provider_key)).mcp_url,
                    headers=new_headers),
                timeout=OAUTH_REFRESH_ATTACH_TIMEOUT)
        except Exception:
            _log.exception("failed to attach SDK server after refresh for %s", conn.runtime_name)
```

(`oauth_pending_sweep` keeps working via `MCPPendingRepo.sweep_expired`; update its import.)

Update the scheduler call site (wherever `oauth_token_refresh` is scheduled in `jarvis/scheduler/scheduler.py`) if it passes args by name — it already passes `flow`, `mcp_manager`, `session_factory`; no change needed unless it referenced `OAUTH_CATALOG`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_oauth_jobs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/scheduler/oauth_jobs.py tests/integration/test_oauth_jobs.py
git commit -m "feat(mcp): proactive refresh iterates connections by runtime_name"
```

---

## Task 10: Wire `ProviderCatalog` into bootstrap; rekey routes/template to keep `/mcp` working

**Files:**
- Modify: `jarvis/main.py` (build catalog, seed, pass to flow/manager/scheduler, add to `AppContext`)
- Modify: `jarvis/web/routes/oauth.py`, `jarvis/web/routes/mcp.py`
- Modify: `jarvis/web/templates/mcp.html` (data shape only; still one card per provider)
- Modify: `tests/integration/test_web_oauth.py`, `tests/integration/test_web_mcp.py`

This task restores a green app end-to-end with the new model, preserving the existing single-account UX.

- [ ] **Step 1: Write the failing route test**

```python
# tests/integration/test_web_mcp_connections.py
from unittest.mock import MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
        await MCPConnectionRepo(s).create(provider_key="gmail", label="Default",
                                          runtime_name="gmail:default")
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_mcp_page_lists_providers_and_connections(client):
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "Gmail" in resp.text
    assert "Default" in resp.text  # connection label
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_web_mcp_connections.py -v`
Expected: FAIL — route still imports `OAUTH_CATALOG`/`OAuthCredentialsRepo`.

- [ ] **Step 3: Rewrite `routes/mcp.py` data assembly**

```python
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo

@router.get("/mcp", response_class=HTMLResponse)
async def mcp_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        providers = await MCPProviderRepo(session).list_all()
        connections = await MCPConnectionRepo(session).list_all()
        servers = await MCPServerRepo(session).list_all()
        server_tools = {srv.id: await MCPToolRepo(session).list_for_server(srv.id) for srv in servers}
    runtime_by_name = {s.name: s for s in servers}
    # Group connections under providers; attach runtime status by runtime_name.
    conns_by_provider: dict[str, list] = {}
    for c in connections:
        rt = runtime_by_name.get(c.runtime_name)
        conns_by_provider.setdefault(c.provider_key, []).append({
            "id": str(c.id), "label": c.label, "runtime_name": c.runtime_name,
            "enabled": c.enabled, "auth_status": c.status, "last_error": c.last_error,
            "authorized": c.access_token_enc is not None,
            "runtime_status": rt.status if rt else "disconnected",
            "tools": server_tools.get(rt.id, []) if rt else [],
        })
    provider_views = [{
        "key": p.key, "display_name": p.display_name, "kind": p.kind, "builtin": p.builtin,
        "connections": conns_by_provider.get(p.key, []),
    } for p in providers]
    stdio_servers = [s for s in servers if s.source == "stdio"]
    return templates.TemplateResponse(request, "mcp.html", {
        "providers": provider_views, "stdio_servers": stdio_servers, "server_tools": server_tools,
    })
```

(Keep the `set_tool_policy` POST handler unchanged.)

- [ ] **Step 4: Rewrite `routes/oauth.py` to connection_id**

- `GET /oauth/connect/{connection_id}`: load connection, 404 if missing; `consent_url = await ctx.oauth_flow.start_authorization(UUID(connection_id))`.
- `GET /oauth/callback`: `result = handle_callback(...)`; `headers = current_headers(result.connection_id)`; attach via `ctx.mcp_manager.replace_oauth_server(result.runtime_name, url=(await ctx.catalog.get(result.provider_key)).mcp_url, headers=headers)`; on failure set connection status needs_reauth via `MCPConnectionRepo.set_status(result.connection_id, ...)`.
- `POST /oauth/disconnect/{connection_id}`: `await ctx.mcp_manager.remove_oauth_server(conn.runtime_name)`; `await ctx.oauth_flow.revoke(conn.id)`; `MCPConnectionRepo.clear_tokens(conn.id)`. Redirect `/mcp`.

(Replace all `OAUTH_CATALOG` imports with `ctx.catalog`.)

- [ ] **Step 5: Rewrite the OAuth section of `mcp.html`**

Replace the `oauth_cards` loop with a providers loop rendering each provider and its connections (Connect link → `/oauth/connect/{conn.id}`, Disconnect form → `/oauth/disconnect/{conn.id}`), and the servers loop with `stdio_servers`. Keep the tool table markup (now rendered per connection from `conn.tools` and per stdio server). Full template is delivered in Phase 2; for Phase 1 the minimal version below keeps the page green:

```html
<section class="section-block">
  <h2>Providers</h2>
  {% for p in providers %}
    <div class="provider-row">
      <h3>{{ p.display_name }} <span class="badge">{{ p.kind }}</span></h3>
      {% for c in p.connections %}
        <div class="conn-row">
          <strong>{{ c.label }}</strong>
          <span class="badge {% if c.runtime_status == 'connected' %}badge-ok{% elif c.runtime_status == 'error' %}badge-err{% else %}badge-warn{% endif %}">{{ c.runtime_status }}</span>
          {% if p.kind == 'oauth' %}
            {% if not c.authorized or c.auth_status == 'needs_reauth' %}
              <a class="btn" href="/oauth/connect/{{ c.id }}">Connect</a>
            {% else %}
              <form method="post" action="/oauth/disconnect/{{ c.id }}" class="inline-form"><button>Disconnect</button></form>
            {% endif %}
          {% endif %}
          {% if c.last_error %}<pre>{{ c.last_error }}</pre>{% endif %}
        </div>
      {% endfor %}
    </div>
  {% endfor %}
</section>
<section class="section-block">
  <h2>stdio servers</h2>
  {% for srv in stdio_servers %}
    <div class="server-block">
      <h3>{{ srv.name }} <span class="badge {% if srv.status == 'connected' %}badge-ok{% elif srv.status == 'error' %}badge-err{% else %}badge-warn{% endif %}">{{ srv.status }}</span></h3>
    </div>
  {% endfor %}
</section>
```

- [ ] **Step 6: Wire bootstrap (`main.py`)**

After `create_all` + `seed_built_in_digest_templates`, add:

```python
from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
async with factory() as session:
    await seed_built_in_providers(session)
catalog = ProviderCatalog(factory)
```

Pass `catalog=catalog` to `OAuthFlow(...)`, `MCPManager(...)`. Add `catalog: ProviderCatalog` to `AppContext` and set `catalog=catalog`. The scheduler reads `mcp_manager._catalog` already (Task 9), so no extra arg needed; if `scheduler.py` constructs the refresh job, leave as-is.

- [ ] **Step 7: Update `test_web_oauth.py` / `test_web_mcp.py`**

Replace `oauth_credentials` seeding with provider+connection seeding; set `ctx.catalog = ProviderCatalog(factory)`; update connect/disconnect URLs to use `connection_id`. Run both files and fix.

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (no references to `OAUTH_CATALOG`/`OAuthCredentialsRepo`/`OAuthPendingRepo` remain). Run `grep -rn "OAUTH_CATALOG\|OAuthCredentialsRepo\|OAuthPendingRepo\|OAuthPendingRow\|OAuthCredentialsRow" jarvis` → expect no hits.

- [ ] **Step 9: Commit**

```bash
git add jarvis/main.py jarvis/web/routes/mcp.py jarvis/web/routes/oauth.py jarvis/web/templates/mcp.html tests/integration/test_web_mcp_connections.py tests/integration/test_web_oauth.py tests/integration/test_web_mcp.py
git commit -m "feat(mcp): wire ProviderCatalog into app; rekey /mcp + oauth routes to connections"
```

---

## Task 11: Alembic migration `0011` (schema + data) + idempotent bootstrap seed

**Files:**
- Create: `alembic/versions/0011_provider_connection_model.py`
- Test: `tests/integration/test_migration_0011.py`

The bootstrap path uses `create_all` (fresh/test DBs get tables from models) + `seed_built_in_providers` (Task 10). The migration covers **existing deployments**: create tables, seed providers, convert `oauth_credentials`→`default` connections, import env creds, drop `oauth_credentials`.

- [ ] **Step 1: Write the failing migration test**

```python
# tests/integration/test_migration_0011.py
"""0011 creates provider/connection tables, seeds builtin providers, migrates oauth_credentials."""
import base64
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
        capture_output=True, text=True, cwd=cwd, env=env or {**os.environ})


def test_0011_creates_tables_and_seeds_providers(tmp_path):
    db_path = tmp_path / "test.db"
    env = {**os.environ, "JARVIS_SECRETS_KEY": generate_key()}
    r = _run_alembic(db_path, "upgrade 0011", env=env)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(db_path); cur = conn.cursor()
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
    conn = sqlite3.connect(db_path); cur = conn.cursor()
    now = datetime.now(UTC).isoformat()
    cur.execute(
        "INSERT INTO oauth_credentials (provider_key, client_id_enc, client_secret_enc, "
        "access_token_enc, refresh_token_enc, token_expires_at, scopes_granted, status, "
        "last_error, connected_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("gmail", f.encrypt(b"CID"), f.encrypt(b"SEC"), f.encrypt(b"AT"), f.encrypt(b"RT"),
         now, "[]", "connected", None, now, now))
    conn.commit(); conn.close()

    assert _run_alembic(db_path, "upgrade 0011", env=env).returncode == 0
    conn = sqlite3.connect(db_path); cur = conn.cursor()
    cur.execute("SELECT provider_key, label, runtime_name, client_id_enc, access_token_enc, status "
                "FROM mcp_connections")
    rows = cur.fetchall(); conn.close()
    assert len(rows) == 1
    pk, label, rt, cid_enc, at_enc, status = rows[0]
    assert pk == "gmail" and rt == "gmail:default" and status == "connected"
    assert f.decrypt(cid_enc) == b"CID" and f.decrypt(at_enc) == b"AT"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_migration_0011.py -v`
Expected: FAIL — `Can't locate revision '0011'`.

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/0011_provider_connection_model.py
"""provider/connection model

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-13 00:00:00.000000
"""
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from cryptography.fernet import Fernet

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels = None
depends_on = None

# Definition-only seed; mirrors jarvis.oauth.catalog.SEED_PROVIDERS. Duplicated here
# so the migration is hermetic (migrations must not import app code that may drift).
_SEED = [
    dict(key="fastmail", display_name="Fastmail", kind="oauth",
         mcp_url="https://api.fastmail.com/mcp", auth_mode="dcr",
         oauth_metadata_url="https://api.fastmail.com/.well-known/oauth-authorization-server",
         default_scopes=[], extra_auth_params={}),
    dict(key="gmail", display_name="Gmail", kind="oauth",
         mcp_url="https://gmailmcp.googleapis.com/mcp/v1", auth_mode="manual",
         oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
         default_scopes=["https://www.googleapis.com/auth/gmail.readonly",
                         "https://www.googleapis.com/auth/gmail.compose"],
         extra_auth_params={"access_type": "offline", "prompt": "consent"}),
    dict(key="calendar", display_name="Google Calendar", kind="oauth",
         mcp_url="https://calendarmcp.googleapis.com/mcp/v1", auth_mode="manual",
         oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
         default_scopes=["https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                         "https://www.googleapis.com/auth/calendar.events.freebusy",
                         "https://www.googleapis.com/auth/calendar.events.readonly"],
         extra_auth_params={"access_type": "offline", "prompt": "consent"}),
]
_GOOGLE_PROVIDERS = {"gmail", "calendar"}


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)

    op.create_table(
        "mcp_providers",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mcp_url", sa.Text(), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auth_mode", sa.String(16), nullable=True),
        sa.Column("oauth_metadata_url", sa.Text(), nullable=True),
        sa.Column("pkce", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("send_resource_indicator", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("extra_auth_params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("default_scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("header_names", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "mcp_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider_key", sa.String(64), sa.ForeignKey("mcp_providers.key", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("runtime_name", sa.String(255), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("client_id_enc", sa.LargeBinary(), nullable=True),
        sa.Column("client_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes_granted", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("url_override", sa.Text(), nullable=True),
        sa.Column("headers_enc", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="disconnected"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "mcp_pending",
        sa.Column("state", sa.String(64), primary_key=True),
        sa.Column("connection_id", sa.Uuid(), sa.ForeignKey("mcp_connections.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.add_column("mcp_servers", sa.Column("source", sa.String(16), nullable=False, server_default="stdio"))
    op.add_column("mcp_servers", sa.Column("connection_id", sa.Uuid(), nullable=True))

    providers = sa.table("mcp_providers", *[sa.column(c) for c in (
        "key", "display_name", "kind", "mcp_url", "builtin", "auth_mode", "oauth_metadata_url",
        "pkce", "send_resource_indicator", "extra_auth_params", "default_scopes", "header_names",
        "created_at", "updated_at")])
    for p in _SEED:
        op.bulk_insert(providers, [dict(
            key=p["key"], display_name=p["display_name"], kind=p["kind"], mcp_url=p["mcp_url"],
            builtin=True, auth_mode=p["auth_mode"], oauth_metadata_url=p["oauth_metadata_url"],
            pkce=True, send_resource_indicator=True, extra_auth_params=p["extra_auth_params"],
            default_scopes=p["default_scopes"], header_names=[], created_at=now, updated_at=now)])

    # Migrate existing oauth_credentials -> one 'default' connection each.
    insp = sa.inspect(bind)
    if "oauth_credentials" in insp.get_table_names():
        legacy = bind.execute(sa.text(
            "SELECT provider_key, client_id_enc, client_secret_enc, access_token_enc, "
            "refresh_token_enc, token_expires_at, scopes_granted, status, last_error, connected_at "
            "FROM oauth_credentials")).fetchall()
        scopes_by_key = {p["key"]: p["default_scopes"] for p in _SEED}
        conns = sa.table("mcp_connections", *[sa.column(c) for c in (
            "id", "provider_key", "label", "runtime_name", "enabled", "client_id_enc",
            "client_secret_enc", "scopes", "access_token_enc", "refresh_token_enc",
            "token_expires_at", "scopes_granted", "url_override", "headers_enc", "status",
            "last_error", "connected_at", "created_at", "updated_at")])
        import json as _json
        for row in legacy:
            m = row._mapping
            op.bulk_insert(conns, [dict(
                id=uuid.uuid4(), provider_key=m["provider_key"], label="Default",
                runtime_name=f"{m['provider_key']}:default", enabled=True,
                client_id_enc=m["client_id_enc"], client_secret_enc=m["client_secret_enc"],
                scopes=scopes_by_key.get(m["provider_key"], []),
                access_token_enc=m["access_token_enc"], refresh_token_enc=m["refresh_token_enc"],
                token_expires_at=m["token_expires_at"],
                scopes_granted=_json.loads(m["scopes_granted"]) if isinstance(m["scopes_granted"], str) else (m["scopes_granted"] or []),
                url_override=None, headers_enc=None, status=m["status"], last_error=m["last_error"],
                connected_at=m["connected_at"], created_at=now, updated_at=now)])
        op.drop_table("oauth_credentials")
    else:
        # Fresh DB built by create_all that already lacks oauth_credentials — nothing to migrate.
        pass

    # Import Google app creds from env once, onto the gmail/calendar default connections
    # that have no client yet (i.e. no legacy row supplied one).
    secrets_key = os.environ.get("JARVIS_SECRETS_KEY")
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    sec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if secrets_key and cid:
        f = Fernet(secrets_key.encode())
        cid_enc = f.encrypt(cid.encode())
        sec_enc = f.encrypt(sec.encode()) if sec else None
        for pkey in _GOOGLE_PROVIDERS:
            rt = f"{pkey}:default"
            existing = bind.execute(sa.text(
                "SELECT id, client_id_enc FROM mcp_connections WHERE runtime_name = :rt"),
                {"rt": rt}).fetchone()
            if existing is None:
                bind.execute(sa.text(
                    "INSERT INTO mcp_connections (id, provider_key, label, runtime_name, enabled, "
                    "client_id_enc, client_secret_enc, scopes, scopes_granted, status, created_at, updated_at) "
                    "VALUES (:id,:pk,'Default',:rt,1,:cid,:sec,'[]','[]','disconnected',:now,:now)"),
                    {"id": str(uuid.uuid4()), "pk": pkey, "rt": rt, "cid": cid_enc, "sec": sec_enc, "now": now})
            elif existing._mapping["client_id_enc"] is None:
                bind.execute(sa.text(
                    "UPDATE mcp_connections SET client_id_enc=:cid, client_secret_enc=:sec WHERE id=:id"),
                    {"cid": cid_enc, "sec": sec_enc, "id": existing._mapping["id"]})


def downgrade() -> None:
    op.drop_column("mcp_servers", "connection_id")
    op.drop_column("mcp_servers", "source")
    op.drop_table("mcp_pending")
    op.drop_table("mcp_connections")
    op.drop_table("mcp_providers")
    # oauth_credentials/oauth_pending are NOT recreated on downgrade (one-way data migration).
```

> Note: the `_SEED` list is intentionally duplicated from `SEED_PROVIDERS` because Alembic migrations must be hermetic and not import app code that may change. A unit test (Step 5) guards that they stay in sync.

- [ ] **Step 4: Run the migration test**

Run: `uv run pytest tests/integration/test_migration_0011.py -v`
Expected: PASS.

- [ ] **Step 5: Add a sync-guard test**

```python
# add to tests/unit/test_oauth_catalog.py
def test_migration_seed_matches_catalog():
    import importlib.util, pathlib
    from jarvis.oauth.catalog import SEED_PROVIDERS
    path = pathlib.Path("alembic/versions/0011_provider_connection_model.py")
    spec = importlib.util.spec_from_file_location("m0011", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mig_keys = {p["key"] for p in mod._SEED}
    assert mig_keys == set(SEED_PROVIDERS)
    for p in mod._SEED:
        entry = SEED_PROVIDERS[p["key"]]
        assert p["mcp_url"] == entry.mcp_url
        assert list(p["default_scopes"]) == list(entry.default_scopes)
        assert p["auth_mode"] == entry.auth_mode.value
```

Run: `uv run pytest tests/unit/test_oauth_catalog.py::test_migration_seed_matches_catalog -v`
Expected: PASS.

- [ ] **Step 6: Full suite + grep gate**

Run: `uv run pytest -q` → PASS.
Run: `grep -rn "OAUTH_CATALOG\|OAuthCredentials\|OAuthPending\|GOOGLE_OAUTH_CLIENT" jarvis | grep -v "alembic"` → only allowed hits are none in `jarvis/` (env import lives in the migration). Expect no app-code hits.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/0011_provider_connection_model.py tests/integration/test_migration_0011.py tests/unit/test_oauth_catalog.py
git commit -m "feat(mcp): alembic 0011 — provider/connection tables, seed, oauth_credentials migration, env import"
```

---

## Phase 1 self-review checklist (run before handing off)

- [ ] `grep -rn "OAUTH_CATALOG\|OAuthCredentialsRepo\|OAuthCredentialsRow\|OAuthPendingRepo\|OAuthPendingRow" jarvis` → no hits.
- [ ] `uv run pytest -q` → all green.
- [ ] `uv run alembic -x db_url=sqlite+aiosqlite:///$(mktemp -u).db upgrade head` → succeeds.
- [ ] Manual smoke (optional): boot app, open `/mcp`, confirm Gmail/Calendar/Fastmail providers render each with a `Default` connection; Connect link points to `/oauth/connect/<uuid>`.

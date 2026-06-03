# Dynamic Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user pick the LLM model at runtime — a global interactive model (Discord `/model` slash command + dashboard) and an optional per-schedule model — discovered from the endpoint's `/v1/models`, persisted in the DB, with hybrid handling when a selected model disappears.

**Architecture:** Model resolution stays per-run and network-free: each `AgentRunner` gets a `model_provider` callable, and `ScheduledTrigger.model` overrides it for pinned schedules. A `ModelCatalog` lazily lists `/v1/models` (30s TTL cache); a `ModelStore` (backed by the existing `settings` table) holds the interactive selection with the YAML `llm.model` as the default. Scheduled runs auto-fall-back to the default when a pinned model is confirmed gone; interactive (Discord) runs fail loud with a DM.

**Tech Stack:** Python 3.12, pydantic, SQLAlchemy (async) + Alembic, FastAPI + Jinja2, discord.py 2.7, OpenAI Agents SDK, openai AsyncOpenAI, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-03-dynamic-model-selection-design.md`

---

## File Structure

New files:
- `jarvis/agents/model_catalog.py` — `Catalog` result + `ModelCatalog` (lists `/v1/models`, TTL cache).
- `jarvis/agents/model_store.py` — `ModelStore` (interactive selection, persisted, default fallback).
- `jarvis/channels/discord_commands.py` — `ModelCommandDeps`, pure text handlers, auth gate, `register_model_commands`.
- `alembic/versions/0004_schedule_model.py` — adds `schedules.model`.
- Tests mirroring each (`tests/unit/...`, `tests/integration/...`).

Modified:
- `jarvis/core/types.py` — `ScheduledTrigger.model`; `AuditEventType.MODEL_CHANGED`, `MODEL_FALLBACK`.
- `jarvis/persistence/models.py` — `ScheduleRow.model`.
- `jarvis/persistence/repositories.py` — `ScheduleRepo.create(model=...)`.
- `jarvis/agents/runner.py` — `model_provider` + `resolve_model` helper.
- `jarvis/scheduler/scheduler.py` — carry `row.model`; catalog pre-check + auto-fallback audit.
- `jarvis/channels/discord_adapter.py` — `CommandTree` registration, deps, error-reply DM.
- `jarvis/web/routes/settings.py` + `templates/settings.html` — model dropdown, POST, ⚠ badge.
- `jarvis/web/routes/schedules.py` + `templates/schedules.html` — model field, ⚠ badge.
- `jarvis/main.py` — store client, build catalog + store, wire providers + adapter deps.
- `README.md` — Discord DM-context note + usage.

---

## Task 1: Audit event types + `ScheduledTrigger.model`

**Files:**
- Modify: `jarvis/core/types.py`
- Test: `tests/unit/test_core_types.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_core_types.py`:

```python
def test_scheduled_trigger_model_defaults_none_and_accepts_value():
    from jarvis.core.types import ScheduledTrigger

    t = ScheduledTrigger(schedule_id="s1", prompt="p", output_mode="discord")
    assert t.model is None

    t2 = ScheduledTrigger(
        schedule_id="s1", prompt="p", output_mode="discord", model="gpt-4o"
    )
    assert t2.model == "gpt-4o"


def test_model_audit_event_types_exist():
    from jarvis.core.types import AuditEventType

    assert AuditEventType.MODEL_CHANGED.value == "model.changed"
    assert AuditEventType.MODEL_FALLBACK.value == "model.fallback"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_core_types.py::test_model_audit_event_types_exist tests/unit/test_core_types.py::test_scheduled_trigger_model_defaults_none_and_accepts_value -v`
Expected: FAIL (`AttributeError: MODEL_CHANGED` / unexpected keyword `model`).

- [ ] **Step 3: Add the audit types**

In `jarvis/core/types.py`, inside `class AuditEventType(StrEnum)`, add after `CONFIG_RELOAD_FAILED = "config.reload_failed"`:

```python
    MODEL_CHANGED = "model.changed"
    MODEL_FALLBACK = "model.fallback"
```

- [ ] **Step 4: Add the `model` field to `ScheduledTrigger`**

In `jarvis/core/types.py`, in `class ScheduledTrigger(_ModelBase)`, add a field after `output_mode`:

```python
    model: str | None = None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_core_types.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/core/types.py tests/unit/test_core_types.py
git commit -m "feat: add model audit event types and ScheduledTrigger.model"
```

---

## Task 2: `ScheduleRow.model` column + repo support

**Files:**
- Modify: `jarvis/persistence/models.py`, `jarvis/persistence/repositories.py`
- Test: `tests/integration/test_repositories_audit_schedule.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_repositories_audit_schedule.py` (it already builds an in-memory DB + `ScheduleRepo`; reuse its existing fixture — inspect the top of the file for the fixture name, commonly `factory`/`session`). Add a self-contained test:

```python
async def test_schedule_create_persists_model(tmp_path):
    from jarvis.persistence.db import Base, create_engine, session_factory
    from jarvis.persistence.repositories import ScheduleRepo

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    async with factory() as s:
        repo = ScheduleRepo(s)
        with_model = await repo.create(
            name="a", description="", cron_expr="* * * * *", timezone="UTC",
            prompt="p", output_mode="discord", notify_on_error=True, enabled=True,
            model="gpt-4o",
        )
        without_model = await repo.create(
            name="b", description="", cron_expr="* * * * *", timezone="UTC",
            prompt="p", output_mode="discord", notify_on_error=True, enabled=True,
        )

    async with factory() as s:
        repo = ScheduleRepo(s)
        a = await repo.get(with_model.id)
        b = await repo.get(without_model.id)
        assert a.model == "gpt-4o"
        assert b.model is None

    await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py::test_schedule_create_persists_model -v`
Expected: FAIL (`TypeError: create() got an unexpected keyword 'model'`).

- [ ] **Step 3: Add the column**

In `jarvis/persistence/models.py`, in `class ScheduleRow(Base)`, add after `output_mode`:

```python
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
```

- [ ] **Step 4: Add the `model` param to `ScheduleRepo.create`**

In `jarvis/persistence/repositories.py`, `ScheduleRepo.create`, add `model: str | None = None` to the signature (after `enabled: bool`) and `model=model,` to the `ScheduleRow(...)` constructor call.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py::test_schedule_create_persists_model -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/models.py jarvis/persistence/repositories.py tests/integration/test_repositories_audit_schedule.py
git commit -m "feat: add model column to schedules table and ScheduleRepo.create"
```

---

## Task 3: Alembic migration 0004 (schedules.model)

**Files:**
- Create: `alembic/versions/0004_schedule_model.py`
- Test: `tests/integration/test_schedule_model_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_schedule_model_migration.py`:

```python
"""Alembic migration 0004: schedules.model column exists after upgrade, gone after downgrade."""

import os
import sqlite3
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["uv", "run", "alembic", "-x", f"db_url=sqlite+aiosqlite:///{db_path}", *cmd.split()],
        capture_output=True, text=True, cwd=cwd, env={**os.environ},
    )


def _cols(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info('schedules')")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def test_upgrade_adds_model_column(tmp_path):
    db_path = tmp_path / "t.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr
    assert "model" in _cols(db_path)


def test_downgrade_removes_model_column(tmp_path):
    db_path = tmp_path / "t.db"
    assert _run_alembic(db_path, "upgrade head").returncode == 0
    down = _run_alembic(db_path, "downgrade -1")
    assert down.returncode == 0, down.stderr
    assert "model" not in _cols(db_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_schedule_model_migration.py -v`
Expected: FAIL (upgrade reaches head `0003`, no `model` column).

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0004_schedule_model.py`:

```python
"""add schedules.model

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(sa.Column("model", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("model")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_schedule_model_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0004_schedule_model.py tests/integration/test_schedule_model_migration.py
git commit -m "feat: alembic migration adding schedules.model column"
```

---

## Task 4: `ModelCatalog` (list `/v1/models`, TTL cache)

**Files:**
- Create: `jarvis/agents/model_catalog.py`
- Test: `tests/unit/test_model_catalog.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_model_catalog.py`:

```python
from types import SimpleNamespace

import pytest

from jarvis.agents.model_catalog import Catalog, ModelCatalog


class _FakeModels:
    def __init__(self, ids, *, raise_exc=None):
        self._ids = ids
        self._raise = raise_exc
        self.calls = 0

    async def list(self):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return SimpleNamespace(data=[SimpleNamespace(id=i) for i in self._ids])


class _FakeClient:
    def __init__(self, models):
        self.models = models


@pytest.mark.asyncio
async def test_list_models_returns_sorted_ok():
    client = _FakeClient(_FakeModels(["zeta", "alpha", "mid"]))
    cat = ModelCatalog(client)
    result = await cat.list_models()
    assert isinstance(result, Catalog)
    assert result.ok is True
    assert result.models == ["alpha", "mid", "zeta"]


@pytest.mark.asyncio
async def test_list_models_error_returns_not_ok_empty():
    client = _FakeClient(_FakeModels([], raise_exc=RuntimeError("boom")))
    cat = ModelCatalog(client)
    result = await cat.list_models()
    assert result.ok is False
    assert result.models == []


@pytest.mark.asyncio
async def test_success_is_cached_within_ttl_and_refetched_after():
    fake = _FakeModels(["a"])
    client = _FakeClient(fake)
    t = {"now": 1000.0}
    cat = ModelCatalog(client, ttl_sec=30.0, clock=lambda: t["now"])

    await cat.list_models()
    await cat.list_models()
    assert fake.calls == 1  # second served from cache

    t["now"] = 1031.0
    await cat.list_models()
    assert fake.calls == 2  # TTL expired -> refetch


@pytest.mark.asyncio
async def test_failure_is_not_cached():
    fake = _FakeModels([], raise_exc=RuntimeError("down"))
    client = _FakeClient(fake)
    t = {"now": 0.0}
    cat = ModelCatalog(client, ttl_sec=30.0, clock=lambda: t["now"])
    await cat.list_models()
    await cat.list_models()
    assert fake.calls == 2  # failures retried every call
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_model_catalog.py -v`
Expected: FAIL (`ModuleNotFoundError: jarvis.agents.model_catalog`).

- [ ] **Step 3: Write the implementation**

Create `jarvis/agents/model_catalog.py`:

```python
"""ModelCatalog — lists models from the LLM endpoint's /v1/models.

Fetched on-demand only, with a short TTL cache so dashboard renders and
Discord autocomplete keystrokes don't hammer the endpoint. Successful results
are cached; failures are not (so recovery is immediate). The `ok` flag lets
callers distinguish "model confirmed absent" from "couldn't reach the endpoint"
— load-bearing for the hybrid stale-model fallback.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Catalog:
    models: list[str]
    ok: bool


class ModelCatalog:
    def __init__(
        self,
        client,
        *,
        ttl_sec: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_sec
        self._clock = clock
        self._cached: Catalog | None = None
        self._cached_at: float = 0.0

    async def list_models(self) -> Catalog:
        now = self._clock()
        if self._cached is not None and (now - self._cached_at) < self._ttl:
            return self._cached
        try:
            resp = await self._client.models.list()
            ids = sorted(m.id for m in resp.data)
            cat = Catalog(models=ids, ok=True)
        except Exception:
            _log.warning("failed to list models from endpoint", exc_info=True)
            return Catalog(models=[], ok=False)
        self._cached = cat
        self._cached_at = now
        return cat
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_model_catalog.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/agents/model_catalog.py tests/unit/test_model_catalog.py
git commit -m "feat: ModelCatalog lists /v1/models with TTL cache"
```

---

## Task 5: `ModelStore` (persisted interactive selection)

**Files:**
- Create: `jarvis/agents/model_store.py`
- Test: `tests/integration/test_model_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_model_store.py`:

```python
import pytest_asyncio

from jarvis.agents.model_store import ModelStore
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_current_falls_back_to_default_when_unset(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    assert store.selection() is None
    assert store.current() == "cfg-model"


async def test_set_specific_then_current(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    await store.set("gpt-4o")
    assert store.selection() == "gpt-4o"
    assert store.current() == "gpt-4o"


async def test_set_none_clears_override(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    await store.set("gpt-4o")
    await store.set(None)
    assert store.selection() is None
    assert store.current() == "cfg-model"


async def test_selection_persists_across_reload(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    await store.set("llama-3.1")

    store2 = ModelStore(session_factory=factory, default_model="cfg-model")
    await store2.load()
    assert store2.current() == "llama-3.1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_model_store.py -v`
Expected: FAIL (`ModuleNotFoundError: jarvis.agents.model_store`).

- [ ] **Step 3: Write the implementation**

Create `jarvis/agents/model_store.py`:

```python
"""ModelStore — the active interactive model selection.

Persisted in the `settings` table under one key. `None` (or absent) means
"use the default" (the YAML config model). `current()` and `selection()` are
sync and read an in-memory cache so model resolution stays off the DB hot path;
`load()` primes the cache at boot and `set()` writes through.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.persistence.repositories import SettingsRepo

_KEY = "llm.active_model"


class ModelStore:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        default_model: str,
    ) -> None:
        self._session_factory = session_factory
        self._default = default_model
        self._selection: str | None = None

    async def load(self) -> None:
        async with self._session_factory() as session:
            value = await SettingsRepo(session).get(_KEY)
        self._selection = value if isinstance(value, str) else None

    def selection(self) -> str | None:
        """The raw stored override, or None when set to default."""
        return self._selection

    def current(self) -> str:
        """The resolved model: the override, or the config default."""
        return self._selection or self._default

    async def set(self, model: str | None) -> None:
        """Set the override; None clears it back to the default."""
        async with self._session_factory() as session:
            await SettingsRepo(session).set(_KEY, model)
        self._selection = model
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_model_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/agents/model_store.py tests/integration/test_model_store.py
git commit -m "feat: ModelStore for persisted interactive model selection"
```

---

## Task 6: `AgentRunner` model resolution (`resolve_model` + `model_provider`)

**Files:**
- Modify: `jarvis/agents/runner.py`
- Test: `tests/unit/test_resolve_model.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_resolve_model.py`:

```python
from jarvis.agents.runner import resolve_model
from jarvis.core.types import ChannelKind, ChannelMessage, ScheduledTrigger


def _channel():
    return ChannelMessage(
        channel_kind=ChannelKind.DISCORD, channel_ref="1", text="hi", external_id="x"
    )


def _scheduled(model=None):
    return ScheduledTrigger(schedule_id="s", prompt="p", output_mode="discord", model=model)


def test_explicit_override_wins():
    sentinel = object()
    got = resolve_model(_scheduled("pinned"), explicit=sentinel,
                        model_provider=lambda: "prov", config_default="cfg")
    assert got is sentinel


def test_scheduled_trigger_model_used_when_set():
    got = resolve_model(_scheduled("pinned"), explicit=None,
                        model_provider=lambda: "prov", config_default="cfg")
    assert got == "pinned"


def test_scheduled_without_model_uses_provider():
    got = resolve_model(_scheduled(None), explicit=None,
                        model_provider=lambda: "prov", config_default="cfg")
    assert got == "prov"


def test_channel_trigger_uses_provider():
    got = resolve_model(_channel(), explicit=None,
                        model_provider=lambda: "prov", config_default="cfg")
    assert got == "prov"


def test_falls_back_to_config_default_without_provider():
    got = resolve_model(_channel(), explicit=None,
                        model_provider=None, config_default="cfg")
    assert got == "cfg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_resolve_model.py -v`
Expected: FAIL (`ImportError: cannot import name 'resolve_model'`).

- [ ] **Step 3: Add `resolve_model` and wire the constructor**

In `jarvis/agents/runner.py`:

Add `Callable` is already imported. Add a module-level function (e.g. just below `_extract_text` or near the bottom helpers):

```python
def resolve_model(trigger, *, explicit, model_provider, config_default):
    """Pick the model for a run. Precedence: explicit (test override) >
    a scheduled trigger's pinned model > model_provider() > config default."""
    if explicit is not None:
        return explicit
    if isinstance(trigger, ScheduledTrigger) and trigger.model:
        return trigger.model
    if model_provider is not None:
        return model_provider()
    return config_default
```

In `AgentRunner.__init__`, add a parameter `model_provider: Callable[[], str] | None = None` (after `model`) and store `self._model_provider = model_provider`.

Replace the model-resolution block in `run()`:

```python
        if self._model is not None:
            agent_kwargs["model"] = self._model
        else:
            agent_kwargs["model"] = self._llm_config.model
```

with:

```python
        agent_kwargs["model"] = resolve_model(
            request.trigger,
            explicit=self._model,
            model_provider=self._model_provider,
            config_default=self._llm_config.model,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_resolve_model.py tests/integration/test_agent_runner.py -v`
Expected: PASS (existing runner tests still pass — they use `model=_FakeModel()`, the highest-priority `explicit` path).

- [ ] **Step 5: Commit**

```bash
git add jarvis/agents/runner.py tests/unit/test_resolve_model.py
git commit -m "feat: AgentRunner model_provider + resolve_model precedence"
```

---

## Task 7: Scheduler carries `row.model` + hybrid auto-fallback

**Files:**
- Modify: `jarvis/scheduler/scheduler.py`
- Test: `tests/integration/test_scheduler_model.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_scheduler_model.py` (reuses the `_FakeModel` + infra pattern from `test_scheduler.py`):

```python
import asyncio

import pytest_asyncio
from agents import set_trace_processors
from agents.models.interface import Model

from jarvis.agents.model_catalog import Catalog
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.types import AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo, ScheduleRepo
from jarvis.scheduler.scheduler import Scheduler


class _FakeModel(Model):
    async def get_response(self, *a, **kw):
        from agents.items import ModelResponse, Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        return ModelResponse(
            output=[ResponseOutputMessage(
                id="m1", type="message", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text="ok", annotations=[])],
            )],
            usage=Usage(), response_id=None,
        )

    async def stream_response(self, *a, **kw):
        if False:
            yield None


class _StubCatalog:
    def __init__(self, result: Catalog):
        self._result = result
    async def list_models(self) -> Catalog:
        return self._result


@pytest_asyncio.fixture(loop_scope="function")
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    set_trace_processors([JarvisTraceProcessor(audit)])
    yield factory, audit
    await audit.stop()
    await engine.dispose()


async def _make_scheduler(factory, audit, catalog):
    return Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="cfg-model"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
        model_catalog=catalog,
    )


async def test_pinned_model_absent_triggers_fallback_audit(infra):
    factory, audit = infra
    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="x", description="", cron_expr="* * * * *", timezone="UTC",
            prompt="p", output_mode="dashboard_only", notify_on_error=True,
            enabled=True, model="ghost-model",
        )
    catalog = _StubCatalog(Catalog(models=["cfg-model", "other"], ok=True))
    scheduler = await _make_scheduler(factory, audit, catalog)
    await scheduler.fire_now(sched.id)
    await asyncio.sleep(0.15)

    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    fb = [e for e in events if e.type == AuditEventType.MODEL_FALLBACK.value]
    assert len(fb) == 1
    assert fb[0].payload["requested"] == "ghost-model"
    assert fb[0].payload["substituted"] == "cfg-model"


async def test_pinned_model_present_no_fallback(infra):
    factory, audit = infra
    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="x", description="", cron_expr="* * * * *", timezone="UTC",
            prompt="p", output_mode="dashboard_only", notify_on_error=True,
            enabled=True, model="other",
        )
    catalog = _StubCatalog(Catalog(models=["cfg-model", "other"], ok=True))
    scheduler = await _make_scheduler(factory, audit, catalog)
    await scheduler.fire_now(sched.id)
    await asyncio.sleep(0.15)
    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    assert not [e for e in events if e.type == AuditEventType.MODEL_FALLBACK.value]


async def test_catalog_unavailable_no_fallback(infra):
    factory, audit = infra
    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="x", description="", cron_expr="* * * * *", timezone="UTC",
            prompt="p", output_mode="dashboard_only", notify_on_error=True,
            enabled=True, model="ghost-model",
        )
    catalog = _StubCatalog(Catalog(models=[], ok=False))
    scheduler = await _make_scheduler(factory, audit, catalog)
    await scheduler.fire_now(sched.id)
    await asyncio.sleep(0.15)
    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    assert not [e for e in events if e.type == AuditEventType.MODEL_FALLBACK.value]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_scheduler_model.py -v`
Expected: FAIL (`Scheduler.__init__() got an unexpected keyword 'model_catalog'`).

- [ ] **Step 3: Add `model_catalog` to the Scheduler**

In `jarvis/scheduler/scheduler.py`:

Add imports near the top (alongside the existing `from jarvis.core.types import ScheduledTrigger`):

```python
from jarvis.core.types import AuditEvent, AuditEventType, ScheduledTrigger
```

In `Scheduler.__init__`, add a parameter `model_catalog=None` (after `model_override`) and store both `self._model_catalog = model_catalog` and `self._llm_config = llm_config` (the existing `llm_config` param is currently only forwarded to the runner, not stored — we need it for the fallback audit payload).

- [ ] **Step 4: Apply the fallback + carry the model in `_execute_schedule`**

In `_execute_schedule`, replace this block:

```python
            prompt = row.prompt
            output_mode = row.output_mode

        trigger = ScheduledTrigger(
            schedule_id=str(schedule_id),
            prompt=prompt,
            output_mode=output_mode,
        )
```

with:

```python
            prompt = row.prompt
            output_mode = row.output_mode
            model = row.model

        if model is not None and self._model_catalog is not None:
            catalog = await self._model_catalog.list_models()
            if catalog.ok and model not in catalog.models:
                await self._audit.emit(
                    AuditEvent(
                        type=AuditEventType.MODEL_FALLBACK,
                        payload={
                            "schedule_id": str(schedule_id),
                            "requested": model,
                            "substituted": self._llm_config.model,
                        },
                    )
                )
                model = None  # None -> runner falls back to the config default

        trigger = ScheduledTrigger(
            schedule_id=str(schedule_id),
            prompt=prompt,
            output_mode=output_mode,
            model=model,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_scheduler_model.py tests/integration/test_scheduler.py -v`
Expected: PASS (existing scheduler tests unaffected — they pass no `model_catalog`, so the fallback branch is skipped).

- [ ] **Step 6: Commit**

```bash
git add jarvis/scheduler/scheduler.py tests/integration/test_scheduler_model.py
git commit -m "feat: scheduler carries per-schedule model with hybrid auto-fallback"
```

---

## Task 8: Discord `/model` command logic (pure handlers + auth gate)

**Files:**
- Create: `jarvis/channels/discord_commands.py`
- Test: `tests/unit/test_discord_commands.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_discord_commands.py`:

```python
import pytest

from jarvis.agents.model_catalog import Catalog
from jarvis.channels.discord_commands import (
    ModelCommandDeps,
    is_authorized,
    model_current_text,
    model_list_text,
    model_set_text,
)


def _deps(*, models=("a", "b"), ok=True, active=("a", False), captured=None):
    async def list_models():
        return Catalog(models=list(models), ok=ok)

    async def set_active(sel):
        if captured is not None:
            captured.append(sel)

    return ModelCommandDeps(
        list_models=list_models,
        get_active_model=lambda: active,
        set_active_model=set_active,
    )


def test_is_authorized():
    assert is_authorized("111", {"111", "222"})
    assert not is_authorized("999", {"111"})


@pytest.mark.asyncio
async def test_current_reports_override_vs_default():
    text = await model_current_text(_deps(active=("gpt-4o", True)))
    assert "gpt-4o" in text and "override" in text.lower()
    text2 = await model_current_text(_deps(active=("cfg", False)))
    assert "cfg" in text2 and "default" in text2.lower()


@pytest.mark.asyncio
async def test_list_ok_and_failure():
    text = await model_list_text(_deps(models=["m1", "m2"], ok=True))
    assert "m1" in text and "m2" in text
    bad = await model_list_text(_deps(models=[], ok=False))
    assert "couldn't" in bad.lower() or "could not" in bad.lower()


@pytest.mark.asyncio
async def test_set_specific_and_default_sentinel():
    captured = []
    text = await model_set_text(_deps(captured=captured), "gpt-4o")
    assert captured == ["gpt-4o"]
    assert "gpt-4o" in text

    captured2 = []
    text2 = await model_set_text(_deps(captured=captured2), "default")
    assert captured2 == [None]
    assert "default" in text2.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_discord_commands.py -v`
Expected: FAIL (`ModuleNotFoundError: jarvis.channels.discord_commands`).

- [ ] **Step 3: Write the implementation**

Create `jarvis/channels/discord_commands.py`:

```python
"""Discord `/model` command: pure text handlers, an auth gate, and registration.

The text-producing handlers are split out (no discord types) so they're unit
testable without a live gateway. `register_model_commands` wires them onto a
CommandTree with the allow-list gate and autocomplete.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord
from discord import app_commands

from jarvis.agents.model_catalog import Catalog

_log = logging.getLogger(__name__)

_DEFAULT_SENTINEL = "default"


@dataclass(slots=True)
class ModelCommandDeps:
    list_models: Callable[[], Awaitable[Catalog]]
    get_active_model: Callable[[], tuple[str, bool]]  # (model, is_override)
    set_active_model: Callable[[str | None], Awaitable[None]]


def is_authorized(user_id: str, allowed: set[str]) -> bool:
    return user_id in allowed


async def model_current_text(deps: ModelCommandDeps) -> str:
    model, is_override = deps.get_active_model()
    suffix = "override" if is_override else "default from config"
    return f"Active interactive model: `{model}` ({suffix})."


async def model_list_text(deps: ModelCommandDeps) -> str:
    cat = await deps.list_models()
    if not cat.ok:
        return (
            "⚠ Couldn't load models from the endpoint. "
            "Try again, or set one manually with `/model set`."
        )
    if not cat.models:
        return "No models reported by the endpoint."
    return "Available models:\n" + "\n".join(f"- `{m}`" for m in cat.models)


async def model_set_text(deps: ModelCommandDeps, name: str) -> str:
    cleaned = name.strip()
    sel = None if cleaned == "" or cleaned.lower() == _DEFAULT_SENTINEL else cleaned
    await deps.set_active_model(sel)
    if sel is None:
        return "Interactive model reset to the config default."
    return f"Interactive model set to `{sel}`. Takes effect on the next message."


def register_model_commands(
    tree: app_commands.CommandTree,
    *,
    allowed: set[str],
    deps: ModelCommandDeps,
) -> None:
    """Attach the `/model` group to `tree`."""

    group = app_commands.Group(name="model", description="Inspect or change the LLM model")
    # Make the group usable in DMs for user-installed apps.
    group.allowed_installs = app_commands.AppInstallationType(guild=True, user=True)
    group.allowed_contexts = app_commands.AppCommandContext(
        guild=True, dm_channel=True, private_channel=True
    )

    async def _guard(interaction: discord.Interaction) -> bool:
        if not is_authorized(str(interaction.user.id), allowed):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return False
        return True

    @group.command(name="current", description="Show the active interactive model")
    async def current_cmd(interaction: discord.Interaction) -> None:
        if not await _guard(interaction):
            return
        await interaction.response.send_message(await model_current_text(deps), ephemeral=True)

    @group.command(name="list", description="List available models")
    async def list_cmd(interaction: discord.Interaction) -> None:
        if not await _guard(interaction):
            return
        await interaction.response.send_message(await model_list_text(deps), ephemeral=True)

    @group.command(name="set", description="Set the interactive model")
    @app_commands.describe(name="Model id, or 'default' for the config model")
    async def set_cmd(interaction: discord.Interaction, name: str) -> None:
        if not await _guard(interaction):
            return
        await interaction.response.send_message(await model_set_text(deps, name), ephemeral=True)

    @set_cmd.autocomplete("name")
    async def _set_autocomplete(interaction: discord.Interaction, current: str):
        cat = await deps.list_models()
        choices = [app_commands.Choice(name="default (config model)", value=_DEFAULT_SENTINEL)]
        for m in cat.models:
            if current.lower() in m.lower():
                choices.append(app_commands.Choice(name=m, value=m))
        return choices[:25]

    tree.add_command(group)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_discord_commands.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/channels/discord_commands.py tests/unit/test_discord_commands.py
git commit -m "feat: discord /model command logic (handlers, gate, registration)"
```

---

## Task 9: Discord adapter — register commands, deps, error-reply DM

**Files:**
- Modify: `jarvis/channels/discord_adapter.py`
- Test: `tests/integration/test_discord_adapter_receive.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_discord_adapter_receive.py`:

```python
async def test_dispatch_failure_dms_user():
    class _BoomDispatcher:
        async def dispatch_channel_message(self, msg, *, allowed_refs):
            raise RuntimeError("model unavailable")

    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = _BoomDispatcher()

    msg = _make_dm_message(content="hello", author_id=111, message_id=7)
    sent = []
    async def _send(text):
        sent.append(text)
    msg.channel.send = _send

    await adapter._on_message(msg)

    assert len(sent) == 1
    assert "/model set" in sent[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_discord_adapter_receive.py::test_dispatch_failure_dms_user -v`
Expected: FAIL (no DM sent — current `except` only logs).

- [ ] **Step 3: Add the error reply**

In `jarvis/channels/discord_adapter.py`, in `_on_message`, replace:

```python
        try:
            await self._dispatcher.dispatch_channel_message(ch_msg, allowed_refs=self._allowed)
        except Exception:
            _log.exception("discord dispatch failed")
```

with:

```python
        try:
            await self._dispatcher.dispatch_channel_message(ch_msg, allowed_refs=self._allowed)
        except Exception:
            _log.exception("discord dispatch failed")
            try:
                await message.channel.send(
                    "⚠ Couldn't process that — the selected model may be "
                    "unavailable. Pick another with `/model set`."
                )
            except Exception:
                _log.exception("failed to send discord error reply")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_discord_adapter_receive.py -v`
Expected: PASS (all receive tests).

- [ ] **Step 5: Wire command registration into the adapter**

In `jarvis/channels/discord_adapter.py`:

Add import near the top:

```python
from discord import app_commands

from jarvis.channels.discord_commands import ModelCommandDeps, register_model_commands
```

Add `model_command_deps: ModelCommandDeps | None = None` to `DiscordAdapter.__init__` (after `allowed_user_ids`) and store `self._model_command_deps = model_command_deps`. Also add an instance field `self._tree: app_commands.CommandTree | None = None`.

In `_build_client`, after `client = self._client_factory()` and before the event handlers, add:

```python
        if self._model_command_deps is not None:
            tree = app_commands.CommandTree(client)
            register_model_commands(
                tree, allowed=self._allowed, deps=self._model_command_deps
            )
            self._tree = tree
```

In the `on_ready` handler inside `_build_client`, after `self._ready.set()`, add a best-effort sync:

```python
            if self._tree is not None:
                try:
                    await self._tree.sync()
                except Exception:
                    _log.exception("failed to sync discord application commands")
```

- [ ] **Step 6: Verify adapter still imports/constructs and tests pass**

Run: `uv run pytest tests/integration/test_discord_adapter_receive.py tests/integration/test_discord_adapter_send.py tests/integration/test_discord_adapter_supervision.py -v`
Expected: PASS (existing tests construct the adapter without `model_command_deps`, so command registration is skipped).

- [ ] **Step 7: Commit**

```bash
git add jarvis/channels/discord_adapter.py tests/integration/test_discord_adapter_receive.py
git commit -m "feat: discord adapter registers /model commands and DMs on run failure"
```

---

## Task 10: Dashboard — `/settings` model dropdown + POST + badge

**Files:**
- Modify: `jarvis/web/routes/settings.py`, `jarvis/web/templates/settings.html`
- Test: `tests/integration/test_web_settings.py`

- [ ] **Step 1: Write the failing test**

Replace the contents of `tests/integration/test_web_settings.py` with:

```python
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from jarvis.agents.model_catalog import Catalog
from jarvis.web.app import create_app


def _mock_context(*, selection=None, ok=True, models=("alpha", "beta")):
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://x/v1"
    ctx.config.jarvis.llm.model = "cfg-model"
    ctx.config.jarvis.timezone = "UTC"
    ctx.config.jarvis.idle_timeout_sec = 900
    ctx.config.jarvis.max_concurrent_agents = 3
    ctx.config.jarvis.log_level = "INFO"
    ctx.config.channels.discord = None
    ctx.config.mcp_servers.servers = []

    ctx.model_store.selection.return_value = selection
    ctx.model_store.current.return_value = selection or "cfg-model"
    ctx.model_store.set = AsyncMock()
    ctx.model_catalog.list_models = AsyncMock(return_value=Catalog(models=list(models), ok=ok))
    ctx.audit.emit = AsyncMock()
    return ctx


def test_settings_page_lists_models(tmp_path):
    ctx = _mock_context()
    client = TestClient(create_app(app_context=ctx))
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "alpha" in resp.text and "beta" in resp.text
    assert "UTC" in resp.text


def test_settings_page_flags_unavailable_selection():
    ctx = _mock_context(selection="ghost", ok=True, models=("alpha",))
    client = TestClient(create_app(app_context=ctx))
    resp = client.get("/settings")
    assert "not available" in resp.text.lower()


def test_post_model_sets_specific():
    ctx = _mock_context()
    client = TestClient(create_app(app_context=ctx))
    resp = client.post("/settings/model", data={"model": "alpha"}, follow_redirects=False)
    assert resp.status_code in (302, 303)
    ctx.model_store.set.assert_awaited_once_with("alpha")
    ctx.audit.emit.assert_awaited_once()


def test_post_model_empty_clears_to_default():
    ctx = _mock_context()
    client = TestClient(create_app(app_context=ctx))
    resp = client.post("/settings/model", data={"model": ""}, follow_redirects=False)
    assert resp.status_code in (302, 303)
    ctx.model_store.set.assert_awaited_once_with(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_web_settings.py -v`
Expected: FAIL (no `/settings/model` route; template lacks model list).

- [ ] **Step 3: Update the route**

Replace `jarvis/web/routes/settings.py` with:

```python
"""GET /settings — config view with live model selection; POST /settings/model."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.core.types import AuditEvent, AuditEventType

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    cfg = ctx.config

    catalog = await ctx.model_catalog.list_models()
    selection = ctx.model_store.selection()
    current = ctx.model_store.current()
    selection_unavailable = (
        selection is not None and catalog.ok and selection not in catalog.models
    )

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "jarvis": cfg.jarvis,
            "channels": cfg.channels,
            "mcp_servers": cfg.mcp_servers,
            "available_models": catalog.models,
            "catalog_ok": catalog.ok,
            "model_selection": selection,
            "model_current": current,
            "config_model": cfg.jarvis.llm.model,
            "selection_unavailable": selection_unavailable,
        },
    )


@router.post("/settings/model")
async def set_model(request: Request, model: str = Form("")):
    ctx = request.app.state.ctx
    old = ctx.model_store.current()
    sel = model.strip() or None
    await ctx.model_store.set(sel)
    await ctx.audit.emit(
        AuditEvent(
            type=AuditEventType.MODEL_CHANGED,
            payload={"old": old, "new": ctx.model_store.current(), "source": "dashboard"},
        )
    )
    return RedirectResponse(url="/settings", status_code=303)
```

- [ ] **Step 4: Update the template**

In `jarvis/web/templates/settings.html`, replace the single row:

```html
    <tr><th>LLM model</th><td>{{ jarvis.llm.model }}</td></tr>
```

with:

```html
    <tr>
        <th>LLM model</th>
        <td>
            <form method="post" action="/settings/model" style="display:inline">
                <select name="model">
                    <option value="" {% if not model_selection %}selected{% endif %}>
                        Default (from config: {{ config_model }})
                    </option>
                    {% for m in available_models %}
                        <option value="{{ m }}" {% if model_selection == m %}selected{% endif %}>{{ m }}</option>
                    {% endfor %}
                    {% if selection_unavailable %}
                        <option value="{{ model_selection }}" selected>{{ model_selection }} (not available)</option>
                    {% endif %}
                </select>
                <button type="submit">Set</button>
            </form>
            {% if not catalog_ok %}
                <span class="muted">— couldn't load model list; type-only via config</span>
            {% endif %}
            {% if selection_unavailable %}
                <span class="badge badge-warn">selected model not available</span>
            {% endif %}
        </td>
    </tr>
```

Also remove (or leave) the trailing "Settings are read-only" note's relevance — update it to:

```html
<p class="muted">Most settings are read-only (edit YAML). The active model can be changed above.</p>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_web_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/routes/settings.py jarvis/web/templates/settings.html tests/integration/test_web_settings.py
git commit -m "feat: dashboard model selector on /settings with availability badge"
```

---

## Task 11: Dashboard — schedule create form model field + badge

**Files:**
- Modify: `jarvis/web/routes/schedules.py`, `jarvis/web/templates/schedules.html`
- Test: `tests/integration/test_web_schedules.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_web_schedules.py`. First extend the fixture's mock ctx so the route can fetch the catalog — update the `client_and_factory` fixture's ctx setup by adding (just after `ctx.session_factory = factory`):

```python
    from unittest.mock import AsyncMock

    from jarvis.agents.model_catalog import Catalog

    ctx.model_catalog.list_models = AsyncMock(return_value=Catalog(models=["alpha", "beta"], ok=True))
```

Then add tests:

```python
def test_schedules_page_lists_models_in_form(client_and_factory):
    client, _ = client_and_factory
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert "alpha" in resp.text and "beta" in resp.text


def test_create_schedule_with_model(client_and_factory):
    client, factory = client_and_factory
    resp = client.post(
        "/schedules",
        data={
            "name": "pinned", "description": "", "cron_expr": "0 8 * * *",
            "timezone": "UTC", "prompt": "do it", "output_mode": "discord",
            "model": "alpha",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_create_schedule_default_model_is_null(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data={
            "name": "unpinned", "description": "", "cron_expr": "0 8 * * *",
            "timezone": "UTC", "prompt": "do it", "output_mode": "discord",
            "model": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_web_schedules.py -v`
Expected: FAIL (form has no model options; route may ignore `model`).

- [ ] **Step 3: Update the route**

In `jarvis/web/routes/schedules.py`:

Update `schedule_list` to pass the catalog:

```python
@router.get("/schedules", response_class=HTMLResponse)
async def schedule_list(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    catalog = await ctx.model_catalog.list_models()
    async with ctx.session_factory() as session:
        schedules = await ScheduleRepo(session).list_all()
    available = set(catalog.models) if catalog.ok else None
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "schedules": schedules,
            "available_models": catalog.models,
            "catalog_ok": catalog.ok,
            "available_set": available,
        },
    )
```

Update `schedule_create` to accept and store `model`:

```python
@router.post("/schedules")
async def schedule_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    cron_expr: str = Form(...),
    timezone: str = Form("UTC"),
    prompt: str = Form(...),
    output_mode: str = Form("discord"),
    model: str = Form(""),
):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        await ScheduleRepo(session).create(
            name=name,
            description=description,
            cron_expr=cron_expr,
            timezone=timezone,
            prompt=prompt,
            output_mode=output_mode,
            notify_on_error=True,
            enabled=True,
            model=model.strip() or None,
        )
    return RedirectResponse(url="/schedules", status_code=303)
```

- [ ] **Step 4: Update the template**

In `jarvis/web/templates/schedules.html`, add a model select to the create form, after the `output_mode` select and before the submit button:

```html
    <select name="model">
        <option value="">Use default model</option>
        {% for m in available_models %}
            <option value="{{ m }}">{{ m }}</option>
        {% endfor %}
    </select>
```

Add a "Model" column header after `<th>Output</th>`:

```html
            <th>Model</th>
```

And the matching cell after the output-mode `<td>{{ s.output_mode }}</td>`:

```html
            <td>
                {% if s.model %}
                    {{ s.model }}
                    {% if available_set is not none and s.model not in available_set %}
                        <span class="badge badge-warn">not available</span>
                    {% endif %}
                {% else %}
                    <span class="muted">default</span>
                {% endif %}
            </td>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_web_schedules.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/routes/schedules.py jarvis/web/templates/schedules.html tests/integration/test_web_schedules.py
git commit -m "feat: per-schedule model selection on dashboard with availability badge"
```

---

## Task 12: Wire everything in `main.py`

**Files:**
- Modify: `jarvis/main.py`
- Test: `tests/integration/test_main_smoke.py`

- [ ] **Step 1: Write the failing test**

The file already has a `config_dir` fixture (writes `jarvis.yaml` with `model: m`, plus empty `channels.yaml` / `mcp-servers.yaml`). Append a new test that reuses it:

```python
async def test_bootstrap_exposes_model_components(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.llm_client is not None
        assert ctx.model_catalog is not None
        assert ctx.model_store is not None
        # Default selection is None -> current() equals the configured model.
        assert ctx.model_store.current() == ctx.config.jarvis.llm.model
    finally:
        await ctx.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: FAIL (`AttributeError: 'AppContext' object has no attribute 'model_catalog'`).

- [ ] **Step 3: Extend `AppContext` and bootstrap wiring**

In `jarvis/main.py`:

Add imports:

```python
from jarvis.agents.model_catalog import ModelCatalog
from jarvis.agents.model_store import ModelStore
from jarvis.channels.discord_commands import ModelCommandDeps
from jarvis.core.types import AuditEvent, AuditEventType
```

Add fields to `AppContext` (after `oauth_http`):

```python
    llm_client: AsyncOpenAI
    model_catalog: ModelCatalog
    model_store: ModelStore
```

(Import `AsyncOpenAI`: `from openai import AsyncOpenAI` at the top.)

In `bootstrap`, change the LLM section:

```python
    # LLM.
    llm_client = build_llm_client(cfg.jarvis.llm)
    install_as_default(llm_client)
    model_catalog = ModelCatalog(llm_client)
    model_store = ModelStore(
        session_factory=factory, default_model=cfg.jarvis.llm.model
    )
    await model_store.load()
```

Change the interactive `agent_runner` construction to add the provider:

```python
    agent_runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=mcp_manager.agent_mcp_servers,
        llm_config=cfg.jarvis.llm,
        model_provider=model_store.current,
        idle_timeout_sec=cfg.jarvis.idle_timeout_sec,
    )
```

Build the Discord command deps and pass them when constructing `DiscordAdapter`:

```python
    async def _set_active_model(model: str | None) -> None:
        old = model_store.current()
        await model_store.set(model)
        await audit.emit(
            AuditEvent(
                type=AuditEventType.MODEL_CHANGED,
                payload={"old": old, "new": model_store.current(), "source": "discord"},
            )
        )

    model_command_deps = ModelCommandDeps(
        list_models=model_catalog.list_models,
        get_active_model=lambda: (model_store.current(), model_store.selection() is not None),
        set_active_model=_set_active_model,
    )

    channel_adapters: list[ChannelAdapter] = []
    if cfg.channels.discord is not None and cfg.channels.discord.enabled:
        discord_adapter = DiscordAdapter(
            token=cfg.channels.discord.token,
            allowed_user_ids=set(cfg.channels.discord.allowed_user_ids),
            model_command_deps=model_command_deps,
        )
        channel_adapters.append(discord_adapter)
```

Pass `model_catalog` to the `Scheduler(...)` construction (add the kwarg):

```python
        model_catalog=model_catalog,
```

Finally, add the three new fields to the `AppContext(...)` instantiation:

```python
        llm_client=llm_client,
        model_catalog=model_catalog,
        model_store=model_store,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/main.py tests/integration/test_main_smoke.py
git commit -m "feat: wire ModelCatalog, ModelStore, providers, and /model deps in bootstrap"
```

---

## Task 13: Full suite + lint, then docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Run the whole test suite**

Run: `uv run pytest -q`
Expected: PASS (all tests). Fix any regressions before continuing.

- [ ] **Step 2: Lint**

Run: `uv run ruff check jarvis tests && uv run ruff format --check jarvis tests`
Expected: clean. If `format --check` fails, run `uv run ruff format jarvis tests`.

- [ ] **Step 3: Document the feature**

In `README.md`, add a short "Model selection" subsection (place it near the configuration/usage section). Content:

```markdown
### Model selection

Jarvis discovers available models from your LLM endpoint's `/v1/models`.

- **Interactive model** (Discord DMs + manual runs): change it from the
  dashboard **Settings** page (model dropdown) or with the Discord
  `/model` command:
  - `/model current` — show the active model
  - `/model list` — list available models
  - `/model set <name>` — set it (autocompletes; choose **default** to use the
    `llm.model` from `jarvis.yaml`)
  The selection is stored in the database and survives restarts.

- **Per-schedule model**: when creating a schedule on the dashboard, pick a
  model (or **Use default model**). Scheduled runs are independent of the
  interactive selection. If a schedule's pinned model is no longer available,
  the run automatically falls back to the `jarvis.yaml` model (recorded in the
  audit log); interactive runs instead reply with an error so you can re-pick.

> **Discord DMs:** for `/model` to appear in DMs, install the application as a
> user-installable app with the DM context enabled in the Discord Developer
> Portal (Installation → User Install). Guild-only installs expose `/model`
> in servers only.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document dynamic model selection (dashboard + /model)"
```

---

## Self-Review Notes (for the implementer)

- **Backward compatibility:** existing `AgentRunner` and `Scheduler` call sites that don't pass `model_provider` / `model_catalog` keep working — resolution falls back to `llm_config.model`, and the scheduler's fallback branch is skipped when `model_catalog is None`. The Discord adapter skips command registration when `model_command_deps is None`. This is why the existing tests for those modules remain green without edits.
- **Network safety in tests:** never let `Runner.run` hit a real endpoint — always pass `model=_FakeModel()` (the `explicit` path) in runner/scheduler tests. Model-string resolution is tested via the pure `resolve_model` helper, not by running the agent.
- **`time.monotonic` in `ModelCatalog`:** injected as `clock` so the TTL test is deterministic; production uses the default.
- **`SettingsRepo.set(key, None)`** stores JSON null; `get` returns `None`, which `ModelStore` treats as "default" — the clear-override path.

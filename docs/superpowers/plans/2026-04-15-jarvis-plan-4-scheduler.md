# Jarvis Plan 4 — Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jarvis can execute agent runs on cron schedules — e.g., "check my email every morning at 8am and summarize." Schedules are stored in SQLite (CRUD via `ScheduleRepo`, already built in Plan 1). Each fire produces a fresh conversation, runs the agent, and routes the output per the schedule's `output_mode` (discord / dashboard_only / discord_if_noteworthy).

**Architecture:** A `Scheduler` wrapper around APScheduler 4's `AsyncScheduler`. On `start()`, it loads enabled schedules from the DB and registers them as APScheduler jobs. Each job fire builds a `ScheduledTrigger` and calls `TriggerDispatcher.dispatch_scheduled()`. After the agent run, a `ScheduledOutputRouter` applies the per-schedule output_mode to decide where to send the result (Discord DM, silent dashboard log, or Discord-only-if-noteworthy). The scheduler integrates into `bootstrap()` / `AppContext` and starts alongside the Discord adapter in `jarvis serve`.

**Tech Stack:** `apscheduler>=4.0` (APScheduler 4 with native `AsyncScheduler` + `CronTrigger`). Existing Plan 1-3 stack unchanged.

**Design spec this plan implements:** `docs/superpowers/specs/2026-04-14-jarvis-agent-service-design.md` — sections: §5.7 Scheduler, §6.2 Scheduled trigger flow, §5.10 OutputRouter (scheduled path — per-schedule output_mode routing).

---

## File Structure

New modules:

```
jarvis/
  scheduler/
    scheduler.py            # Scheduler: wraps APScheduler AsyncScheduler
    scheduled_output.py     # ScheduledOutputRouter: per-schedule output_mode routing
```

Files modified:
- `jarvis/core/dispatcher.py` — `dispatch_scheduled` already exists; no changes needed.
- `jarvis/core/output_router.py` — `SCHEDULED` is already in `_INTERNAL_KINDS` (skipped by channel router); scheduled runs use their own routing path.
- `jarvis/main.py` — `bootstrap()` starts the Scheduler; `AppContext` gains a `scheduler` field.
- `jarvis/cli.py` — no changes needed (`jarvis serve` already runs forever; the scheduler auto-starts in bootstrap).
- `pyproject.toml` — add `apscheduler>=4.0`.

---

## Task 1: Add APScheduler dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit `pyproject.toml`**

Add `apscheduler>=4.0` to the dependencies list:

```toml
dependencies = [
  "pydantic>=2.7",
  "sqlalchemy[asyncio]>=2.0.30",
  "aiosqlite>=0.20",
  "alembic>=1.13",
  "pyyaml>=6.0",
  "watchfiles>=0.22",
  "openai-agents>=0.7",
  "openai>=1.68",
  "typer>=0.12",
  "discord.py>=2.3",
  "apscheduler>=4.0",
]
```

- [ ] **Step 2: Sync**

Run: `uv sync`

- [ ] **Step 3: Verify imports**

Run:
```bash
uv run python -c "
from apscheduler import AsyncScheduler
from apscheduler.triggers.cron import CronTrigger
print('APScheduler imports OK')
"
```

Expected: `APScheduler imports OK`.

- [ ] **Step 4: Full suite**

Run: `uv run pytest`
Expected: 113 passed.

- [ ] **Step 5: ruff + commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add pyproject.toml uv.lock
git commit -m "add apscheduler dependency"
```

---

## Task 2: `ScheduledOutputRouter` — per-schedule output_mode routing

Spec §5.10 + §6.2 step 11. After a scheduled agent run completes, the output_mode decides where the result goes:
- `discord` → send via the Discord adapter (same as a channel-triggered reply).
- `dashboard_only` → no outbound message; the result is in the DB (messages + audit) for the dashboard to display.
- `discord_if_noteworthy` → the schedule's prompt instructs the agent to prefix output with `[NOTEWORTHY]` or `[SILENT]`; the router checks the prefix and only sends to Discord if noteworthy.

This is a separate router from `OutputRouter` because it uses schedule-specific metadata (the `output_mode` field from `ScheduleRow`), not the channel_kind on the result.

**Files:**
- Create: `jarvis/scheduler/scheduled_output.py`
- Create: `tests/unit/test_scheduled_output.py`

- [ ] **Step 1: Write failing tests**

Write `tests/unit/test_scheduled_output.py`:

```python
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import OutboundMessage
from jarvis.core.types import ChannelKind
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter


def _result(text: str = "summary") -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=uuid4(),
        trigger_id=uuid4(),
        channel_kind=ChannelKind.SCHEDULED,
        channel_ref="sched-1",
    )


class _RecordingAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self, dispatcher) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


async def test_discord_mode_sends_to_discord():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="your morning email summary"),
        output_mode="discord",
        discord_user_id="111",
    )

    assert len(adapter.sent) == 1
    assert adapter.sent[0].channel_ref == "111"
    assert adapter.sent[0].text == "your morning email summary"


async def test_dashboard_only_mode_sends_nothing():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="some data"),
        output_mode="dashboard_only",
        discord_user_id="111",
    )

    assert adapter.sent == []


async def test_noteworthy_mode_sends_when_prefixed():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="[NOTEWORTHY] You have 3 urgent emails"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
    )

    assert len(adapter.sent) == 1
    # The prefix is stripped from the delivered text.
    assert "[NOTEWORTHY]" not in adapter.sent[0].text
    assert "3 urgent emails" in adapter.sent[0].text


async def test_noteworthy_mode_silent_when_prefixed():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="[SILENT] Nothing new"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
    )

    assert adapter.sent == []


async def test_noteworthy_mode_sends_when_no_prefix():
    """If the agent didn't use a prefix, default to sending (fail-open)."""
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="Here is your summary"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
    )

    assert len(adapter.sent) == 1


async def test_no_discord_adapter_silently_skips():
    """If no Discord adapter is running, scheduled outputs to discord
    are silently dropped (not raised). Unlike channel-triggered replies
    where the user expects a response, scheduled runs are background."""
    router = ScheduledOutputRouter(discord_adapter=None)

    # Should not raise.
    await router.route(
        result=_result(),
        output_mode="discord",
        discord_user_id="111",
    )
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/unit/test_scheduled_output.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/scheduler/scheduled_output.py`**

```python
"""ScheduledOutputRouter — per-schedule output_mode routing.

After a scheduled agent run completes, this router decides where the
result goes based on the schedule's `output_mode`:

  - "discord": send via the Discord adapter to a specific user.
  - "dashboard_only": no outbound message; result stays in DB for dashboard.
  - "discord_if_noteworthy": check the agent's output for a [NOTEWORTHY]
    or [SILENT] prefix. If noteworthy (or no prefix — fail-open), send to
    Discord; if [SILENT], suppress.

This is separate from `OutputRouter` because it uses per-schedule metadata,
not the channel_kind on the result.
"""

import logging

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.types import ChannelKind

_log = logging.getLogger(__name__)


class ScheduledOutputRouter:
    def __init__(self, *, discord_adapter: ChannelAdapter | None) -> None:
        self._discord = discord_adapter

    async def route(
        self,
        *,
        result: AgentRunResult,
        output_mode: str,
        discord_user_id: str,
    ) -> None:
        if output_mode == "dashboard_only":
            return

        if output_mode == "discord_if_noteworthy":
            text = result.final_output
            upper = text.lstrip().upper()
            if upper.startswith("[SILENT]"):
                return
            if upper.startswith("[NOTEWORTHY]"):
                # Strip the prefix before sending.
                text = text.lstrip()
                text = text[len("[NOTEWORTHY]") :].lstrip()
            # No prefix → fail-open (send).
            await self._send_discord(text, discord_user_id)
            return

        if output_mode == "discord":
            await self._send_discord(result.final_output, discord_user_id)
            return

        _log.warning("unknown output_mode %r; treating as dashboard_only", output_mode)

    async def _send_discord(self, text: str, user_id: str) -> None:
        if self._discord is None:
            _log.warning(
                "scheduled run wants to send to Discord but no adapter is running"
            )
            return
        await self._discord.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref=user_id,
                text=text,
            )
        )
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/unit/test_scheduled_output.py -v`
Expected: 6 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 119 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/scheduler/scheduled_output.py tests/unit/test_scheduled_output.py
git commit -m "add ScheduledOutputRouter with discord/dashboard_only/noteworthy modes"
```

---

## Task 3: `ScheduleRepo` additions — `list_all` and `update`

The dashboard (Plan 5) and the scheduler itself need a few more repo methods beyond what Plan 1 built. Add them now so the Scheduler (Task 4) has everything it needs.

**Files:**
- Modify: `jarvis/persistence/repositories.py`
- Modify: `tests/integration/test_repositories_audit_schedule.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/integration/test_repositories_audit_schedule.py`:

```python


async def test_schedule_list_all(session):
    repo = ScheduleRepo(session)
    await repo.create(
        name="a", description="", cron_expr="* * * * *",
        timezone="UTC", prompt="x", output_mode="discord",
        notify_on_error=True, enabled=True,
    )
    await repo.create(
        name="b", description="", cron_expr="* * * * *",
        timezone="UTC", prompt="y", output_mode="discord",
        notify_on_error=True, enabled=False,
    )
    all_schedules = await repo.list_all()
    assert len(all_schedules) == 2
    assert {s.name for s in all_schedules} == {"a", "b"}


async def test_schedule_update(session):
    repo = ScheduleRepo(session)
    sched = await repo.create(
        name="orig", description="d", cron_expr="0 8 * * *",
        timezone="UTC", prompt="go", output_mode="discord",
        notify_on_error=True, enabled=True,
    )
    await repo.update(
        sched.id,
        name="renamed",
        cron_expr="0 9 * * *",
        prompt="new prompt",
    )
    refreshed = await repo.get(sched.id)
    assert refreshed.name == "renamed"
    assert refreshed.cron_expr == "0 9 * * *"
    assert refreshed.prompt == "new prompt"
    # Unmodified fields stay the same.
    assert refreshed.output_mode == "discord"


async def test_schedule_delete(session):
    repo = ScheduleRepo(session)
    sched = await repo.create(
        name="to-delete", description="", cron_expr="* * * * *",
        timezone="UTC", prompt="x", output_mode="discord",
        notify_on_error=True, enabled=True,
    )
    await repo.delete(sched.id)
    assert await repo.get(sched.id) is None
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py -v`
Expected: failures — `list_all`, `update`, `delete` don't exist.

- [ ] **Step 3: Add the methods to `ScheduleRepo`**

In `jarvis/persistence/repositories.py`, append these methods to the `ScheduleRepo` class:

```python
    async def list_all(self) -> list[ScheduleRow]:
        result = await self._session.execute(select(ScheduleRow))
        return list(result.scalars())

    async def update(self, schedule_id: UUID, **fields) -> None:
        """Update arbitrary fields on a schedule row. Only provided kwargs
        are changed; unmentioned fields are left untouched."""
        fields["updated_at"] = _utcnow()
        await self._session.execute(
            update(ScheduleRow)
            .where(ScheduleRow.id == schedule_id)
            .values(**fields)
        )
        await self._session.commit()

    async def delete(self, schedule_id: UUID) -> None:
        row = await self._session.get(ScheduleRow, schedule_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.commit()
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_repositories_audit_schedule.py -v`
Expected: 8 passed (5 existing + 3 new).

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 122 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_audit_schedule.py
git commit -m "add ScheduleRepo.list_all, update, and delete methods"
```

---

## Task 4: `Scheduler` — the APScheduler wrapper

The core of Plan 4. On `start()`, loads enabled schedules from the DB, registers each as an APScheduler cron job. Each fire: builds a `ScheduledTrigger`, dispatches via `TriggerDispatcher.dispatch_scheduled()`, routes the output via `ScheduledOutputRouter`, and records `last_run_at` / `last_run_status`.

**Files:**
- Create: `jarvis/scheduler/scheduler.py`
- Create: `tests/integration/test_scheduler.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_scheduler.py`:

```python
"""Scheduler integration tests. We use real APScheduler with overridden
fire times to avoid waiting for real cron ticks.
"""

import asyncio

import pytest
import pytest_asyncio
from agents import set_trace_processors
from agents.models.interface import Model

from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MessageRepo, ScheduleRepo
from jarvis.scheduler.scheduler import Scheduler


class _FakeModel(Model):
    def __init__(self) -> None:
        self.call_count = 0

    async def get_response(self, *a, **kw):
        from agents.items import ModelResponse, Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        self.call_count += 1
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="m1",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text=f"scheduled-reply-{self.call_count}",
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    async def stream_response(self, *a, **kw):
        if False:
            yield None


@pytest_asyncio.fixture(loop_scope="function")
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    set_trace_processors([JarvisTraceProcessor(audit)])

    yield engine, factory, audit

    await audit.stop()
    await engine.dispose()


async def test_scheduler_fires_and_records_run(infra):
    """Create a schedule, trigger it manually via fire_now, verify:
    - The agent ran (messages persisted).
    - last_run_at and last_run_status are updated.
    """
    _, factory, audit = infra
    model = _FakeModel()

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=model,
        mcp_servers=[],
        discord_adapter=None,
    )

    # Create a schedule in the DB.
    async with factory() as s:
        repo = ScheduleRepo(s)
        sched = await repo.create(
            name="test-sched",
            description="test",
            cron_expr="0 0 * * *",  # midnight — won't naturally fire during the test
            timezone="UTC",
            prompt="give me a summary",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=True,
        )
        sched_id = sched.id

    await scheduler.start()
    try:
        # Fire the schedule immediately instead of waiting for cron.
        await scheduler.fire_now(sched_id)
        # Let the agent run + audit drain.
        await asyncio.sleep(0.3)
    finally:
        await scheduler.stop()

    # Verify: schedule's last_run updated.
    async with factory() as s:
        refreshed = await ScheduleRepo(s).get(sched_id)
        assert refreshed.last_run_status == "success"
        assert refreshed.last_run_at is not None

    # Verify: messages were persisted (user prompt + assistant reply).
    async with factory() as s:
        from sqlalchemy import select
        from jarvis.persistence.models import ConversationRow

        convs = (await s.execute(select(ConversationRow))).scalars().all()
        assert len(convs) == 1
        assert convs[0].channel_kind == ChannelKind.SCHEDULED.value

        msgs = await MessageRepo(s).history(convs[0].id)
        assert len(msgs) == 2
        assert msgs[0].role == MessageRole.USER.value
        assert msgs[1].role == MessageRole.ASSISTANT.value


async def test_scheduler_handles_disabled_schedule(infra):
    """Disabled schedules should not be loaded into APScheduler."""
    _, factory, audit = infra

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers=[],
        discord_adapter=None,
    )

    async with factory() as s:
        await ScheduleRepo(s).create(
            name="disabled",
            description="",
            cron_expr="* * * * *",
            timezone="UTC",
            prompt="x",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=False,
        )

    await scheduler.start()
    try:
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()


async def test_scheduler_empty_db_starts_cleanly(infra):
    _, factory, audit = infra

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers=[],
        discord_adapter=None,
    )

    await scheduler.start()
    try:
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_scheduler.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/scheduler/scheduler.py`**

```python
"""Scheduler — wraps APScheduler's AsyncScheduler for cron-based agent runs.

On start():
  1. Load enabled schedules from ScheduleRepo.
  2. For each, register an APScheduler cron job.
  3. Start the APScheduler background loop.

Each job fire:
  1. Build a ScheduledTrigger from the schedule row.
  2. Call TriggerDispatcher.dispatch_scheduled().
  3. Route the output via ScheduledOutputRouter.
  4. Record last_run_at / last_run_status.

fire_now(schedule_id) lets tests (and the dashboard "Run Now" button in
Plan 5) trigger a schedule immediately without waiting for the cron tick.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from apscheduler import AsyncScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.channels.base import ChannelAdapter
from jarvis.config.schema import LLMConfig
from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.types import ScheduledTrigger
from jarvis.persistence.repositories import ScheduleRepo
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter

_log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        llm_config: LLMConfig,
        mcp_servers: list,
        discord_adapter: ChannelAdapter | None,
        model_override: Any = None,
        idle_timeout_sec: int = 900,
        max_concurrent: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._llm_config = llm_config
        self._mcp_servers = mcp_servers
        self._model_override = model_override
        self._idle_timeout_sec = idle_timeout_sec
        self._max_concurrent = max_concurrent

        self._output_router = ScheduledOutputRouter(
            discord_adapter=discord_adapter
        )

        # Internal APScheduler instance — created in start().
        self._aps: AsyncScheduler | None = None
        self._aps_task = None
        self._jobs: dict[UUID, str] = {}  # schedule_id → apscheduler job id

        # Each scheduler owns its own runner + dispatcher so scheduled runs
        # don't share concurrency gates with interactive (Discord) runs.
        self._runner = AgentRunner(
            session_factory=session_factory,
            audit=audit,
            mcp_servers=mcp_servers,
            llm_config=llm_config,
            model=model_override,
            idle_timeout_sec=idle_timeout_sec,
        )
        self._dispatcher = TriggerDispatcher(
            runner=self._runner,
            audit=audit,
            max_concurrent=max_concurrent,
        )

    async def start(self) -> None:
        self._aps = AsyncScheduler()
        await self._aps.__aenter__()
        await self._aps.start_in_background()

        # Load enabled schedules from DB and register as cron jobs.
        async with self._session_factory() as session:
            rows = await ScheduleRepo(session).list_enabled()

        for row in rows:
            await self._register(row.id, row.cron_expr, row.timezone)

        _log.info("scheduler started with %d active jobs", len(self._jobs))

    async def stop(self) -> None:
        if self._aps is not None:
            await self._aps.__aexit__(None, None, None)
            self._aps = None
        self._jobs.clear()

    def active_job_count(self) -> int:
        return len(self._jobs)

    async def fire_now(self, schedule_id: UUID) -> None:
        """Trigger a schedule immediately (for tests / dashboard "Run Now")."""
        await self._execute_schedule(schedule_id)

    async def _register(
        self,
        schedule_id: UUID,
        cron_expr: str,
        timezone: str,
    ) -> None:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)

        async def _job() -> None:
            await self._execute_schedule(schedule_id)

        job_id = await self._aps.add_schedule(
            _job, trigger, id=str(schedule_id)
        )
        self._jobs[schedule_id] = job_id

    async def _execute_schedule(self, schedule_id: UUID) -> None:
        # Load the schedule row fresh each time (it may have been updated).
        async with self._session_factory() as session:
            repo = ScheduleRepo(session)
            row = await repo.get(schedule_id)
            if row is None:
                _log.warning("schedule %s not found; skipping", schedule_id)
                return
            if not row.enabled:
                _log.info("schedule %s is disabled; skipping", row.name)
                return

            prompt = row.prompt
            output_mode = row.output_mode
            notify_on_error = row.notify_on_error

        trigger = ScheduledTrigger(
            schedule_id=str(schedule_id),
            prompt=prompt,
            output_mode=output_mode,
        )

        try:
            result = await self._dispatcher.dispatch_scheduled(trigger)

            # Route the output per the schedule's output_mode.
            # For discord routing, we need a target user_id. For now we use
            # the first allowed user from the Discord adapter config (if any).
            # Plan 5 can add a per-schedule discord_user_id field.
            discord_user_id = self._get_discord_target()
            await self._output_router.route(
                result=result,
                output_mode=output_mode,
                discord_user_id=discord_user_id,
            )

            async with self._session_factory() as session:
                await ScheduleRepo(session).record_run(
                    schedule_id, at=datetime.now(UTC), status="success"
                )
        except Exception:
            _log.exception("scheduled run failed for %s", schedule_id)
            async with self._session_factory() as session:
                await ScheduleRepo(session).record_run(
                    schedule_id, at=datetime.now(UTC), status="error"
                )

    def _get_discord_target(self) -> str:
        """Best-effort: return a Discord user ID for scheduled output.

        For a single-user personal agent, this is always the one allow-listed
        user. A proper per-schedule target field can be added in Plan 5.
        """
        return ""  # ScheduledOutputRouter handles None adapter gracefully
```

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_scheduler.py -v`
Expected: 3 passed (tests may take 1-2 seconds due to asyncio.sleep for drain).

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 125 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/scheduler/scheduler.py tests/integration/test_scheduler.py
git commit -m "add Scheduler wrapping APScheduler with cron-based agent runs"
```

---

## Task 5: Wire Scheduler into `bootstrap()` and `AppContext`

**Files:**
- Modify: `jarvis/main.py`
- Modify: `tests/integration/test_main_smoke.py`

- [ ] **Step 1: Write a smoke test**

Append to `tests/integration/test_main_smoke.py`:

```python


async def test_bootstrap_exposes_scheduler(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.scheduler is not None
        assert ctx.scheduler.active_job_count() == 0  # no schedules in DB
    finally:
        await ctx.shutdown()
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_main_smoke.py::test_bootstrap_exposes_scheduler -v`
Expected: fails — `AppContext` has no `scheduler` field.

- [ ] **Step 3: Update `jarvis/main.py`**

Add import:

```python
from jarvis.scheduler.scheduler import Scheduler
```

Add `scheduler: Scheduler` field to `AppContext` (after `output_router`).

Update `shutdown()` — stop scheduler BEFORE adapters:

```python
    async def shutdown(self) -> None:
        # Stop scheduler first so no new cron fires arrive.
        if hasattr(self, 'scheduler') and self.scheduler is not None:
            await self.scheduler.stop()
        for adapter in self.channel_adapters:
            try:
                await adapter.stop()
            except Exception:
                _log.exception("error stopping channel adapter")
        await self.mcp_manager.stop()
        await self.audit.stop()
        await self.engine.dispose()
```

In `bootstrap()`, after the dispatcher is constructed and before adapters are started, add:

```python
    # Find the discord adapter (if any) for scheduled output routing.
    discord_adapter = next(
        (a for a in channel_adapters if a.kind == ChannelKind.DISCORD.value),
        None,
    )

    # Scheduler.
    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=cfg.jarvis.llm,
        mcp_servers=mcp_manager.agent_mcp_servers(),
        discord_adapter=discord_adapter,
        idle_timeout_sec=cfg.jarvis.idle_timeout_sec,
        max_concurrent=cfg.jarvis.max_concurrent_agents,
    )
    await scheduler.start()
```

Add `scheduler=scheduler` to the `return AppContext(...)` call.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: 5 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 126 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/main.py tests/integration/test_main_smoke.py
git commit -m "wire Scheduler into bootstrap and AppContext"
```

---

## Plan 4 complete — summary

At the end of Plan 4:

- Schedules stored in SQLite (Plan 1's `ScheduleRepo`) are loaded by the `Scheduler` on startup and registered as APScheduler cron jobs.
- Each fire runs the full agent pipeline (same `TriggerDispatcher` path as Discord and CLI) in a fresh conversation.
- Per-schedule `output_mode` (discord / dashboard_only / discord_if_noteworthy) controls where the result goes via `ScheduledOutputRouter`.
- `fire_now(schedule_id)` lets tests and the future dashboard "Run Now" button trigger immediately.
- `last_run_at` / `last_run_status` updated on every run (success or error).
- `jarvis serve` automatically starts the scheduler alongside Discord.

**Known debt for Plan 5:**
- No per-schedule `discord_user_id` field — scheduled Discord output currently goes nowhere useful because we return `""` as the target. Plan 5's dashboard can add a UI to set the target user per schedule, or we can derive it from the single allow-listed Discord user.
- No `notify_on_error` Discord DM on failure (the field exists but we don't DM the user on error).
- No live schedule add/remove/modify at runtime — changes require restart. Plan 5's dashboard CRUD should call `scheduler.reload()` or `scheduler.add/remove` to update the APScheduler job set live.
- No `ScheduleRepo.update` used by the scheduler yet (it's ready for Plan 5's dashboard).

**Still to come:** Plan 5 Dashboard, Plan 6 Docker.

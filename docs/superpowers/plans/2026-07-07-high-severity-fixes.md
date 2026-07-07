# High-Severity Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three high-severity review findings: live schedule registration, cron validation + crash-proof boot, and conversation history in agent runs.

**Architecture:** `Scheduler` gains `on_created`/`on_toggled`/`on_deleted` lifecycle methods and a `validate_schedule_timing` helper; the schedule routes call them. `AgentRunner` loads capped prior-turn history before appending the new user message and passes `Runner.run` a structured input list instead of a bare string.

**Tech Stack:** Python 3.12, SQLAlchemy async + SQLite, APScheduler 4 (`AsyncScheduler`), OpenAI Agents SDK, FastAPI, pytest (`asyncio_mode = auto`).

Spec: `docs/superpowers/specs/2026-07-07-high-severity-fixes-design.md`

## Global Constraints

- Use `uv run` for every command; there is no activated venv.
- Pytest runs in `asyncio_mode = auto`: write `async def test_*` with NO `@pytest.mark.asyncio` decorator.
- DB access only through repository classes (`jarvis/persistence/repositories.py`), never raw sessions in feature code.
- All `Mapped[datetime]` values are timezone-aware UTC (`TZDateTime`); binding naive datetimes raises.
- Ruff: line length 100, target py312. Run `uv run ruff check jarvis tests` before each commit.
- Every commit message ends with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- History caps (spec): `_HISTORY_MAX_MESSAGES = 20`, `_HISTORY_MAX_CHARS = 8_000` — module constants in `jarvis/agents/runner.py`.

---

### Task 1: `MessageRepo.recent_history`

**Files:**
- Modify: `jarvis/persistence/repositories.py` (add method to `MessageRepo`, after `history()` around line 168)
- Test: `tests/integration/test_message_recent_history.py` (new file)

**Interfaces:**
- Consumes: existing `MessageRepo`, `ConversationRepo`, `MessageRow`.
- Produces: `async def recent_history(self, conversation_id: UUID, *, limit: int) -> list[MessageRow]` — the newest `limit` messages of the conversation, returned in **chronological** order. Task 2 calls this.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_message_recent_history.py`:

```python
"""MessageRepo.recent_history returns the newest N messages, chronological order."""

from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ConversationRepo, MessageRepo


async def _make_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, session_factory(engine)


async def test_recent_history_returns_newest_in_chronological_order(tmp_path):
    engine, factory = await _make_factory(tmp_path)
    try:
        async with factory() as session:
            conv = await ConversationRepo(session).find_or_create_open(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="42",
                idle_timeout_sec=900,
            )
            repo = MessageRepo(session)
            for i in range(5):
                role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
                await repo.append(conversation_id=conv.id, role=role, content=f"msg-{i}")

            recent = await repo.recent_history(conv.id, limit=3)

        assert [m.content for m in recent] == ["msg-2", "msg-3", "msg-4"]
    finally:
        await engine.dispose()


async def test_recent_history_empty_conversation(tmp_path):
    engine, factory = await _make_factory(tmp_path)
    try:
        async with factory() as session:
            conv = await ConversationRepo(session).find_or_create_open(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="42",
                idle_timeout_sec=900,
            )
            recent = await MessageRepo(session).recent_history(conv.id, limit=20)
        assert recent == []
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_message_recent_history.py -q`
Expected: FAIL with `AttributeError: 'MessageRepo' object has no attribute 'recent_history'`

- [ ] **Step 3: Write minimal implementation**

In `jarvis/persistence/repositories.py`, add to `MessageRepo` directly below the existing `history()` method:

```python
    async def recent_history(self, conversation_id: UUID, *, limit: int) -> list[MessageRow]:
        """Return the newest `limit` messages of a conversation, oldest first."""
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.created_at.desc())
            .limit(limit)
        )
        return list(reversed(list(result.scalars())))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_message_recent_history.py -q`
Expected: 2 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/persistence/repositories.py tests/integration/test_message_recent_history.py
git commit -m "feat: MessageRepo.recent_history for capped conversation history

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: AgentRunner passes structured input with conversation history

**Files:**
- Modify: `jarvis/agents/runner.py`
- Modify: `tests/integration/test_agent_runner.py` (2 existing capture-style tests + new tests)
- Modify: `tests/integration/test_agent_runner_memory.py` (3 existing capture-style tests)

**Interfaces:**
- Consumes: `MessageRepo.recent_history(conversation_id, *, limit)` from Task 1.
- Produces: `Runner.run` is now always called with a `list[dict]` (`[{"role": ..., "content": ...}, ...]`) whose **last** item is the assembled current prompt. Module constants `_HISTORY_MAX_MESSAGES = 20`, `_HISTORY_MAX_CHARS = 8_000`, and helper `_history_input_items(rows) -> list[dict]` (importable for tests).

- [ ] **Step 1: Update the five existing capture-style tests to the new input shape**

Every stub of `jarvis.agents.runner.Runner.run` that does `captured["prompt"] = prompt` must extract the final user message instead — one-line change each, all downstream assertions stay identical.

In `tests/integration/test_agent_runner.py` (two occurrences, in `test_scheduled_trigger_prompt_includes_local_date_context` and `test_agent_runner_prompt_includes_current_mcp_context_before_user_prompt`) and `tests/integration/test_agent_runner_memory.py` (three occurrences at lines ~118, ~159, ~205), change:

```python
    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output="ok")
```

to:

```python
    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt[-1]["content"]
        return SimpleNamespace(final_output="ok")
```

(Keep each stub's original `final_output` value if it differs.)

- [ ] **Step 2: Write the failing tests for history**

Append to `tests/integration/test_agent_runner.py`. Add `ChannelMessage` to the existing `jarvis.core.types` import at the top of the file, and add this import block change only if `ChannelMessage` is not already imported.

```python
async def test_second_message_in_conversation_includes_history(infra, monkeypatch):
    _, factory, audit = infra
    captured = {}

    async def fake_run(agent, run_input, run_config=None):
        captured["input"] = run_input
        return SimpleNamespace(final_output="the assistant reply")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)

    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
        idle_timeout_sec=900,
    )

    def _msg(text, ext_id):
        return InvocationRequest(
            trigger=ChannelMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="42",
                text=text,
                external_id=ext_id,
            )
        )

    await runner.run(_msg("first message", "e1"))
    await runner.run(_msg("second message", "e2"))

    run_input = captured["input"]
    assert isinstance(run_input, list)
    assert run_input[0] == {"role": "user", "content": "first message"}
    assert run_input[1] == {"role": "assistant", "content": "the assistant reply"}
    assert run_input[-1]["role"] == "user"
    assert run_input[-1]["content"].endswith("second message")
    # History must not contain the current turn.
    assert len(run_input) == 3


async def test_first_message_has_no_history(infra, monkeypatch):
    _, factory, audit = infra
    captured = {}

    async def fake_run(agent, run_input, run_config=None):
        captured["input"] = run_input
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)

    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
    )
    await runner.run(
        InvocationRequest(trigger=ManualTrigger(user="dashboard", prompt="hello"))
    )

    run_input = captured["input"]
    assert isinstance(run_input, list)
    assert len(run_input) == 1
    assert run_input[0]["role"] == "user"
    assert run_input[0]["content"].endswith("hello")


def test_history_input_items_drops_oldest_over_char_budget():
    from types import SimpleNamespace as NS

    from jarvis.agents.runner import _HISTORY_MAX_CHARS, _history_input_items

    big = "x" * (_HISTORY_MAX_CHARS - 10)
    rows = [
        NS(role="user", content="oldest"),
        NS(role="assistant", content=big),
        NS(role="user", content="newest"),
    ]
    items = _history_input_items(rows)
    # "oldest" is dropped to fit the budget; newest turns survive.
    assert [i["content"] for i in items] == [big, "newest"]


def test_history_input_items_skips_non_chat_roles():
    from types import SimpleNamespace as NS

    from jarvis.agents.runner import _history_input_items

    rows = [
        NS(role="system", content="internal"),
        NS(role="user", content="hi"),
        NS(role="assistant", content="hello"),
    ]
    items = _history_input_items(rows)
    assert [i["role"] for i in items] == ["user", "assistant"]
```

- [ ] **Step 3: Run new tests to verify they fail**

Run: `uv run pytest tests/integration/test_agent_runner.py -q`
Expected: the new tests FAIL (`ImportError: cannot import name '_history_input_items'` / list-shape assertions), the five updated tests also FAIL (runner still passes a string, so `prompt[-1]["content"]` raises `TypeError: string indices must be integers`).

- [ ] **Step 4: Implement in `jarvis/agents/runner.py`**

4a. Add constants after `_log = logging.getLogger(__name__)`:

```python
_HISTORY_MAX_MESSAGES = 20
_HISTORY_MAX_CHARS = 8_000
```

4b. In `AgentRunner.run`, inside the existing first session block, load history after `conv_id = conv.id` and **before** the `MessageRepo(session).append(...)` call:

```python
            # Prior turns, captured BEFORE the new user message is appended so
            # the current prompt is never duplicated into history.
            history_rows = await MessageRepo(session).recent_history(
                conv_id, limit=_HISTORY_MAX_MESSAGES
            )
```

4c. After `prompt = await self._build_prompt_with_memory(...)`, build the run input:

```python
        run_input: list[dict] = [
            *_history_input_items(history_rows),
            {"role": "user", "content": prompt},
        ]
```

4d. Replace `prompt` with `run_input` in both `Runner.run` calls:

```python
        if self._run_timeout_sec is None:
            sdk_result = await Runner.run(
                agent,
                run_input,
                run_config=RunConfig(workflow_name="jarvis-invoke"),
            )
        else:
            async with asyncio.timeout(self._run_timeout_sec):
                sdk_result = await Runner.run(
                    agent,
                    run_input,
                    run_config=RunConfig(workflow_name="jarvis-invoke"),
                )
```

4e. Add the module-level helper near the other `_`-prefixed helpers at the bottom of the file:

```python
def _history_input_items(rows: list) -> list[dict]:
    """Map prior message rows to SDK chat input items under the char budget.

    Only user/assistant turns are forwarded; oldest items are dropped first
    when total content length exceeds _HISTORY_MAX_CHARS.
    """
    items = [
        {"role": row.role, "content": row.content}
        for row in rows
        if row.role in (MessageRole.USER.value, MessageRole.ASSISTANT.value)
    ]
    total = sum(len(item["content"]) for item in items)
    while items and total > _HISTORY_MAX_CHARS:
        total -= len(items.pop(0)["content"])
    return items
```

(`MessageRole` is already imported in `runner.py`.)

- [ ] **Step 5: Run the runner + memory + scheduler + action test files**

Run: `uv run pytest tests/integration/test_agent_runner.py tests/integration/test_agent_runner_memory.py tests/integration/test_agent_runner_actions.py tests/integration/test_scheduler.py tests/integration/test_dispatcher.py -q`
Expected: all pass. (`test_scheduler.py` and `test_agent_runner.py` message-persistence tests exercise the real `Runner.run`, which accepts a list input natively.)

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/agents/runner.py tests/integration/test_agent_runner.py tests/integration/test_agent_runner_memory.py
git commit -m "feat: pass conversation history to agent runs as structured input

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `validate_schedule_timing` + crash-proof `Scheduler.start()`

**Files:**
- Modify: `jarvis/scheduler/scheduler.py`
- Test: `tests/integration/test_scheduler.py` (append)

**Interfaces:**
- Consumes: existing `Scheduler._register`, `AuditEvent`, `AuditEventType.SCHEDULE_ERROR`.
- Produces: module-level `def validate_schedule_timing(cron_expr: str, timezone: str) -> None` in `jarvis/scheduler/scheduler.py` — raises `ValueError` on any invalid cron/timezone. Task 5's route imports it. `Scheduler.start()` no longer raises when one schedule row is unregisterable.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_scheduler.py`:

```python
def test_validate_schedule_timing_accepts_valid_input():
    from jarvis.scheduler.scheduler import validate_schedule_timing

    validate_schedule_timing("0 8 * * *", "America/Los_Angeles")  # must not raise


def test_validate_schedule_timing_rejects_bad_cron():
    import pytest

    from jarvis.scheduler.scheduler import validate_schedule_timing

    with pytest.raises(ValueError):
        validate_schedule_timing("not a cron", "UTC")


def test_validate_schedule_timing_rejects_bad_timezone():
    import pytest

    from jarvis.scheduler.scheduler import validate_schedule_timing

    with pytest.raises(ValueError):
        validate_schedule_timing("0 8 * * *", "Mars/Olympus_Mons")


async def test_start_survives_bad_schedule_row_and_registers_good_ones(infra):
    _, factory, audit = infra

    async with factory() as s:
        repo = ScheduleRepo(s)
        await repo.create(
            name="bad-cron",
            description="",
            cron_expr="not a cron",
            timezone="UTC",
            prompt="x",
            output_mode="dashboard_only",
            notify_on_error=False,
            enabled=True,
        )
        good = await repo.create(
            name="good",
            description="",
            cron_expr=_far_future_cron_expr(),
            timezone="UTC",
            prompt="x",
            output_mode="dashboard_only",
            notify_on_error=False,
            enabled=True,
        )

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
    )

    await scheduler.start()  # must not raise
    try:
        assert scheduler.active_job_count() == 1
        assert good.id in scheduler._jobs
    finally:
        await scheduler.stop()

    # SCHEDULE_ERROR audit event names the bad schedule.
    await asyncio.sleep(0.1)  # let the audit logger flush
    from jarvis.core.types import AuditEventType
    from jarvis.persistence.repositories import AuditRepo

    async with factory() as s:
        events = await AuditRepo(s).recent(types=[AuditEventType.SCHEDULE_ERROR], limit=10)
    assert any(e.payload.get("schedule_name") == "bad-cron" for e in events)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_scheduler.py -q`
Expected: the three `validate_schedule_timing` tests FAIL with `ImportError`; `test_start_survives_bad_schedule_row_and_registers_good_ones` FAILS because `scheduler.start()` raises `ValueError` from `CronTrigger.from_crontab`.

- [ ] **Step 3: Implement in `jarvis/scheduler/scheduler.py`**

3a. Add the module-level validator below the imports / above the `Scheduler` class:

```python
def validate_schedule_timing(cron_expr: str, timezone: str) -> None:
    """Raise ValueError if `cron_expr`/`timezone` cannot build a CronTrigger.

    Called at schedule-create time so bad input is rejected at the HTTP
    boundary instead of crashing the next boot (Scheduler.start registers
    every enabled row with CronTrigger.from_crontab).
    """
    try:
        CronTrigger.from_crontab(cron_expr, timezone=timezone)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
```

3b. In `Scheduler.start()`, replace the registration loop:

```python
        for row in rows:
            await self._register(row.id, row.cron_expr, row.timezone)
```

with:

```python
        for row in rows:
            try:
                await self._register(row.id, row.cron_expr, row.timezone)
            except Exception as exc:
                # One bad row (e.g. legacy invalid cron) must degrade to one
                # dead schedule, never a dead app.
                _log.exception("failed to register schedule %s (%s)", row.id, row.name)
                await self._audit.emit(
                    AuditEvent(
                        type=AuditEventType.SCHEDULE_ERROR,
                        payload={
                            "schedule_id": str(row.id),
                            "schedule_name": row.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "stage": "register",
                        },
                    )
                )
```

(`AuditEvent` and `AuditEventType` are already imported in `scheduler.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_scheduler.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/scheduler/scheduler.py tests/integration/test_scheduler.py
git commit -m "fix: validate schedule cron/timezone; one bad row no longer crashes boot

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Scheduler lifecycle methods `on_created` / `on_toggled` / `on_deleted`

**Files:**
- Modify: `jarvis/scheduler/scheduler.py`
- Test: `tests/integration/test_scheduler.py` (append)

**Interfaces:**
- Consumes: existing `Scheduler._register`, `self._jobs: dict[UUID, str]`, `self._aps: AsyncScheduler` (APScheduler 4: `await self._aps.remove_schedule(id: str)`).
- Produces (Task 5's routes call these on `ctx.scheduler`):
  - `async def on_created(self, row: ScheduleRow) -> None`
  - `async def on_toggled(self, row: ScheduleRow) -> None` (row reflects the NEW enabled state)
  - `async def on_deleted(self, schedule_id: UUID) -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_scheduler.py`:

```python
async def test_lifecycle_methods_register_and_unregister(infra):
    _, factory, audit = infra

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
    )
    await scheduler.start()
    try:
        assert scheduler.active_job_count() == 0

        async with factory() as s:
            row = await ScheduleRepo(s).create(
                name="post-boot",
                description="",
                cron_expr=_far_future_cron_expr(),
                timezone="UTC",
                prompt="x",
                output_mode="dashboard_only",
                notify_on_error=False,
                enabled=True,
            )

        # Created after boot -> registers live.
        await scheduler.on_created(row)
        assert scheduler.active_job_count() == 1

        # Disable -> unregisters.
        async with factory() as s:
            repo = ScheduleRepo(s)
            await repo.set_enabled(row.id, False)
        async with factory() as s:
            row = await ScheduleRepo(s).get(row.id)
        await scheduler.on_toggled(row)
        assert scheduler.active_job_count() == 0

        # Re-enable -> registers again.
        async with factory() as s:
            await ScheduleRepo(s).set_enabled(row.id, True)
        async with factory() as s:
            row = await ScheduleRepo(s).get(row.id)
        await scheduler.on_toggled(row)
        assert scheduler.active_job_count() == 1

        # Delete -> unregisters.
        await scheduler.on_deleted(row.id)
        assert scheduler.active_job_count() == 0

        # Idempotent: removing an unknown id is a no-op.
        await scheduler.on_deleted(row.id)
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()


async def test_on_created_ignores_disabled_row(infra):
    _, factory, audit = infra

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
    )
    await scheduler.start()
    try:
        async with factory() as s:
            row = await ScheduleRepo(s).create(
                name="starts-disabled",
                description="",
                cron_expr=_far_future_cron_expr(),
                timezone="UTC",
                prompt="x",
                output_mode="dashboard_only",
                notify_on_error=False,
                enabled=False,
            )
        await scheduler.on_created(row)
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_scheduler.py -q`
Expected: the two new tests FAIL with `AttributeError: 'Scheduler' object has no attribute 'on_created'`.

- [ ] **Step 3: Implement in `jarvis/scheduler/scheduler.py`**

Add below `fire_now` (imports for `ScheduleRow` come from `jarvis.persistence.models`; add `from jarvis.persistence.models import ScheduleRow` to the imports):

```python
    async def on_created(self, row: ScheduleRow) -> None:
        """Register a schedule created while the app is running."""
        if row.enabled:
            await self._register(row.id, row.cron_expr, row.timezone)

    async def on_toggled(self, row: ScheduleRow) -> None:
        """Sync APScheduler registration with the row's new enabled state."""
        if row.enabled:
            if row.id not in self._jobs:
                await self._register(row.id, row.cron_expr, row.timezone)
        else:
            await self._unregister(row.id)

    async def on_deleted(self, schedule_id: UUID) -> None:
        """Drop the APScheduler job for a deleted schedule, if registered."""
        await self._unregister(schedule_id)

    async def _unregister(self, schedule_id: UUID) -> None:
        job_id = self._jobs.pop(schedule_id, None)
        if job_id is not None and self._aps is not None:
            await self._aps.remove_schedule(job_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_scheduler.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/scheduler/scheduler.py tests/integration/test_scheduler.py
git commit -m "feat: scheduler lifecycle methods for live schedule registration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire the schedule routes to the scheduler + reject invalid cron with 400

**Files:**
- Modify: `jarvis/web/routes/schedules.py`
- Test: `tests/integration/test_web_schedules.py`

**Interfaces:**
- Consumes: `validate_schedule_timing` (Task 3), `on_created`/`on_toggled`/`on_deleted` (Task 4) via `ctx.scheduler`.
- Produces: `POST /schedules` returns 400 (with the validation reason in `detail`) for invalid cron/timezone and registers valid schedules live; toggle/delete keep APScheduler in sync.

- [ ] **Step 1: Update the test fixture and write the failing tests**

In `tests/integration/test_web_schedules.py`, extend the `client_and_factory` fixture — after `ctx.scheduler.fire_now = AsyncMock(return_value=None)` add:

```python
    ctx.scheduler.on_created = AsyncMock(return_value=None)
    ctx.scheduler.on_toggled = AsyncMock(return_value=None)
    ctx.scheduler.on_deleted = AsyncMock(return_value=None)
```

Append these tests to the same file:

```python
def _create_form(name="lifecycle-sched", cron="0 8 * * *", timezone="UTC"):
    return {
        "name": name,
        "description": "",
        "cron_expr": cron,
        "timezone": timezone,
        "prompt": "x",
        "output_mode": "dashboard_only",
    }


def test_create_schedule_registers_with_live_scheduler(client_and_factory):
    client, _ = client_and_factory
    resp = client.post("/schedules", data=_create_form(), follow_redirects=False)
    assert resp.status_code in (302, 303)

    on_created = client.app.state.ctx.scheduler.on_created
    on_created.assert_awaited_once()
    row = on_created.await_args.args[0]
    assert row.name == "lifecycle-sched"


def test_create_schedule_rejects_invalid_cron(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data=_create_form(name="bad-cron-sched", cron="not a cron"),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    client.app.state.ctx.scheduler.on_created.assert_not_awaited()
    # No row was written.
    assert "bad-cron-sched" not in client.get("/schedules").text


def test_create_schedule_rejects_invalid_timezone(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data=_create_form(name="bad-tz-sched", timezone="Mars/Olympus_Mons"),
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_toggle_schedule_syncs_scheduler(client_and_factory):
    client, _ = client_and_factory
    client.post("/schedules", data=_create_form(name="toggle-sync"), follow_redirects=False)
    on_created = client.app.state.ctx.scheduler.on_created
    schedule_id = on_created.await_args.args[0].id

    resp = client.post(f"/schedules/{schedule_id}/toggle", follow_redirects=False)
    assert resp.status_code in (302, 303)

    on_toggled = client.app.state.ctx.scheduler.on_toggled
    on_toggled.assert_awaited_once()
    row = on_toggled.await_args.args[0]
    assert row.id == schedule_id
    assert row.enabled is False  # was created enabled; toggle flips it


def test_delete_schedule_unregisters(client_and_factory):
    client, _ = client_and_factory
    client.post("/schedules", data=_create_form(name="delete-sync"), follow_redirects=False)
    schedule_id = client.app.state.ctx.scheduler.on_created.await_args.args[0].id

    resp = client.post(f"/schedules/{schedule_id}/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)
    client.app.state.ctx.scheduler.on_deleted.assert_awaited_once_with(schedule_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_web_schedules.py -q`
Expected: the new tests FAIL (`on_created` never awaited; invalid cron currently returns 303).

- [ ] **Step 3: Implement in `jarvis/web/routes/schedules.py`**

3a. Update imports: add `HTTPException` to the fastapi import, and add:

```python
from jarvis.scheduler.scheduler import validate_schedule_timing
```

3b. Replace the body of `schedule_create`:

```python
    ctx = request.app.state.ctx
    try:
        validate_schedule_timing(cron_expr, timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid schedule: {exc}") from exc
    target_user = discord_user_id.strip() or _default_discord_user_id(ctx)
    async with ctx.session_factory() as session:
        row = await ScheduleRepo(session).create(
            name=name,
            description=description,
            cron_expr=cron_expr,
            timezone=timezone,
            prompt=prompt,
            output_mode=output_mode,
            notify_on_error=True,
            enabled=True,
            model=model.strip() or None,
            discord_user_id=target_user,
        )
    await ctx.scheduler.on_created(row)
    return RedirectResponse(url="/schedules", status_code=303)
```

3c. Replace the body of `schedule_toggle` (a fresh session for the re-read is required: `set_enabled` uses a Core `update()`, so the ORM object in the first session still holds the stale `enabled` value):

```python
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = ScheduleRepo(session)
        row = await repo.get(schedule_id)
        if row:
            await repo.set_enabled(schedule_id, not row.enabled)
    if row:
        async with ctx.session_factory() as session:
            row = await ScheduleRepo(session).get(schedule_id)
        await ctx.scheduler.on_toggled(row)
    return RedirectResponse(url="/schedules", status_code=303)
```

3d. Replace the body of `schedule_delete`:

```python
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        await ScheduleRepo(session).delete(schedule_id)
    await ctx.scheduler.on_deleted(schedule_id)
    return RedirectResponse(url="/schedules", status_code=303)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_web_schedules.py -q`
Expected: all pass (including the pre-existing tests, which now go through the AsyncMock lifecycle methods).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check jarvis tests
git add jarvis/web/routes/schedules.py tests/integration/test_web_schedules.py
git commit -m "fix: schedule routes register live and reject invalid cron/timezone

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification

**Files:**
- No new files; fix any fallout uncovered by the full run.

**Interfaces:**
- Consumes: everything above.
- Produces: green `make check`.

- [ ] **Step 1: Run lint + the full test suite**

Run: `make check` (equivalent to `uv run ruff check jarvis tests && uv run pytest -q`)
Expected: 0 lint errors, all tests pass. Likely fallout spots if anything fails: other tests that stub `jarvis.agents.runner.Runner.run` and index into the second argument (`tests/integration/test_agent_runner_actions.py`), and `tests/integration/test_main_smoke.py` / `tests/integration/test_cli.py` which boot the real app.

- [ ] **Step 2: Fix any failures, re-run until green**

Apply minimal fixes in the same style as Task 2 Step 1 (extract `prompt[-1]["content"]` where a test assumed a string input).

- [ ] **Step 3: Commit any fixups**

```bash
git add -A -- ':!CLAUDE.md'
git commit -m "test: adapt remaining Runner.run stubs to structured input

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(Skip the commit if Step 1 was already green with nothing to fix. Never commit the pre-existing uncommitted `CLAUDE.md` working-tree change — it is not part of this branch.)

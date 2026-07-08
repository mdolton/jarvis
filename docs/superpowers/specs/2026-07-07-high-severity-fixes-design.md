# High-severity review fixes: schedule lifecycle + conversation history

Date: 2026-07-07
Status: approved

Three high-severity findings from a full-codebase review, fixed together on one
branch. Fixes 1 and 2 harden the schedule lifecycle; fix 3 gives agent runs
multi-turn context.

## Fix 1 — Dashboard schedule mutations register with the live scheduler

**Problem.** `Scheduler._register` is only called from `Scheduler.start()`.
`POST /schedules` writes the DB row and never registers an APScheduler job, so
new schedules never fire until restart. Re-enabling a schedule that was not
registered at boot (created post-boot, or disabled at boot) also never fires.
Disable only "works" via the fire-time `row.enabled` re-check.

**Design.** `Scheduler` gains three public methods; `self._jobs` remains the
authoritative map of registered jobs:

- `on_created(row)` — register the cron job (wraps `_register`).
- `on_toggled(row)` — enabling: register if not in `_jobs`; disabling: remove
  the APScheduler schedule and drop the `_jobs` entry, so `active_job_count()`
  is truthful.
- `on_deleted(schedule_id)` — remove the APScheduler schedule if registered.

Routes `schedule_create`, `schedule_toggle`, and `schedule_delete` call these
after their DB commit. Scheduler failures in the routes surface as 500s — a
schedule that silently fails to register is exactly the bug being fixed.

## Fix 2 — Cron/timezone validation + crash-proof boot

**Problem.** `schedule_create` accepts arbitrary `cron_expr` / `timezone`
strings. Nothing fails at create time; on the next restart
`CronTrigger.from_crontab` raises inside `Scheduler.start()`, bootstrap fails,
and the container crash-loops.

**Design.** Two ends:

- **Route-side validation.** `validate_schedule_timing(cron_expr, timezone)`
  in `jarvis/scheduler/scheduler.py` tries
  `CronTrigger.from_crontab(cron_expr, timezone=timezone)` and raises
  `ValueError` with the underlying message. `schedule_create` calls it before
  writing and returns HTTP 400 with the reason. Bad input never reaches the DB.
- **Boot-side defense in depth.** `Scheduler.start()` wraps each per-row
  `_register` in try/except: log the exception, emit a `SCHEDULE_ERROR` audit
  event naming the schedule, continue. A bad legacy row degrades to one dead
  schedule instead of a dead app.

## Fix 3 — Conversation history in agent runs

**Problem.** `AgentRunner.run` sends only the current message (plus memory /
runtime context) to `Runner.run`. Prior turns of the open conversation are
persisted but never fed back, so follow-ups like "yes, do that" arrive with no
context.

**Design.** Pass `Runner.run` a structured input list instead of a bare
string. In `AgentRunner.run`, load prior messages *before* appending the new
user message:

```python
input_items = [
    {"role": m.role, "content": m.content}      # prior turns, user/assistant only
    for m in trimmed_history
] + [{"role": "user", "content": assembled_prompt}]  # memory + runtime ctx + new msg
```

- **Loading.** New repo method `MessageRepo.recent_history(conversation_id,
  limit)`: SQL `ORDER BY created_at DESC LIMIT n`, reversed to chronological.
- **Caps.** Last **20 messages**, then an **8,000-character total budget**
  applied by dropping oldest first. Module constants in `runner.py`; no config
  surface yet.
- **Roles.** Only `user` and `assistant` rows are forwarded; anything else is
  skipped.
- The final user message is the existing `assemble_memory_prompt` output,
  unchanged — memory, runtime context, and trigger context ride only on the
  current turn.
- **Unaffected paths.** Scheduled triggers always open a fresh conversation
  (idle timeout 0) so their history is empty. `ActionService` resume is
  untouched — `RunState` carries its own context. Memory summarization still
  receives only the current turn's prompt/output.

## Testing

- **Fix 1** (`tests/unit` scheduler tests): `on_created` registers (job count
  +1); toggle off removes; toggle back on re-registers; delete removes.
  Route-level test: POST `/schedules` registers with the live scheduler.
- **Fix 2**: POST `/schedules` with an invalid cron → 400 and no row written;
  `Scheduler.start()` with one bad row and one good row → starts, registers
  the good one, emits `SCHEDULE_ERROR`.
- **Fix 3**: fake `Runner.run` capture — a second message in an open
  conversation receives prior turns as structured items in order; caps
  enforced (message count and char budget drop oldest); a first message in a
  fresh conversation still works.

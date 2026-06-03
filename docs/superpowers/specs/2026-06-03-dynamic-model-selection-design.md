# Dynamic Model Selection — Design

**Date:** 2026-06-03
**Status:** Approved (pending spec review)

## Goal

Let the user change which LLM model Jarvis uses at runtime, without editing
YAML and restarting:

- Discover available models from the LLM endpoint's `/v1/models`.
- Select the **interactive** model (used by Discord DMs and the dashboard) from
  the dashboard and via a Discord `/model` slash command.
- Choose a **per-schedule** model when creating a schedule on the dashboard,
  independent of the interactive model.

## Background (current state)

- The model is static. `LLMConfig.model` comes from `jarvis.yaml`, lands in a
  frozen `LoadedConfig`, and `AgentRunner` reads `self._llm_config.model` fresh
  on every run (`jarvis/agents/runner.py:118`).
- The `AsyncOpenAI` client is **not** bound to a model — the model string is
  passed per-`Agent`, so "switching models" is just changing that string.
- A key/value `settings` table already exists (`SettingRow` + `SettingsRepo`),
  a natural home for the persisted interactive override.
- The dashboard `/settings` page is read-only (FastAPI + Jinja2). Schedules
  have a create form on `/schedules`.
- The Discord adapter uses a bare `discord.Client` with an `on_message` DM
  handler — **no** slash-command infrastructure exists yet.
- Schedules are created **only** on the dashboard. There is no agent/MCP tool
  for the bot to create schedules, so per-schedule model selection lives on the
  dashboard schedule form. (Bot-driven schedule creation is out of scope.)
- The `AsyncOpenAI` client is built in `main.py` and installed as the Agents
  SDK default but not stored anywhere reachable.

## Decisions

1. **Persistence:** the interactive selection is persisted in the `settings`
   table and survives restarts. The YAML `llm.model` is the fallback default.
2. **Two independent selections, both rooted at the YAML config model:**
   - Interactive (Discord + dashboard) — one global selection.
   - Per-schedule — each schedule may pin its own model.
   The interactive selection **never** affects scheduled runs, and vice versa.
3. **Discord:** a real application slash command (`CommandTree`), subcommand
   group `current` / `list` / `set` with autocomplete on `set`.
4. **When it applies:** changes take effect on the **next** run. In-flight runs
   finish on their current model. No confirmation step.
5. **"Default" is an explicit, selectable option** in both the dashboard
   dropdown and `/model set`, mapping to the YAML config model.
6. **Model list is fetched on-demand with a ~30s TTL cache** — no startup fetch,
   no background poller. New models appear within ~30s of the next picker use.
7. **Stale / unavailable model = Hybrid handling** (see dedicated section):
   scheduled runs auto-fall-back to the config default; interactive runs
   fail loud with a user-visible message; both get a dashboard ⚠ badge.

## Architecture

### Resolution model

The scheduler already builds its **own** `AgentRunner`, separate from the
interactive runner. Rather than one runner juggling two concepts, each runner
receives a single `model_provider` callable wired differently:

- **Interactive runner** (`main.py`): `model_provider = model_store.current`.
- **Scheduler runner**: `model_provider = lambda: config.jarvis.llm.model`
  (always the YAML config model).

`AgentRunner.run` resolves the model as:

```
if self._model is not None:                          # explicit ctor override (tests) — unchanged
    model = self._model
elif isinstance(trigger, ScheduledTrigger) and trigger.model:
    model = trigger.model                            # this schedule's explicit pick
else:
    model = self._model_provider()                   # interactive selection OR config default
```

Consequences:

- Schedule set to **"Default"** → `model = NULL` → scheduler runner's provider →
  **YAML config model**.
- A schedule with a specific model → that model, verbatim.
- Interactive **"Default"** → `ModelStore` override cleared → `current()` returns
  the YAML config model.
- Interactive specific pick → that model, verbatim.

### Stale / unavailable model handling (Hybrid)

Run-time model **resolution** stays network-free and deterministic (above). The
availability policy is layered on top, and differs by run type. A selected model
can only become stale when it is removed *after* selection — pickers are
populated from the catalog, so you normally can't select an unavailable model.

**Pre-existing gaps this design works around (not fixing):**
- The scheduled→Discord output passes an empty `discord_user_id`
  (`scheduler.py:165`) and `notify_on_error` is not consulted in
  `_execute_schedule`. So scheduled runs cannot reliably DM the user today.
- The interactive error path swallows exceptions: `discord_adapter._on_message`
  catches and only logs (`"discord dispatch failed"`); a failed run is silent to
  the user. The dashboard manual-run path is similarly bare.

Given those, Hybrid is realized as:

**Scheduled runs — auto-fall-back (unattended).** In `_execute_schedule`, if
`row.model` is set, consult `ModelCatalog` (cached). If the fetch **succeeds and
confirms the model is gone** (`ok=True and row.model not in models`), substitute
the config default for that run, emit a `MODEL_FALLBACK` audit event, and log
it; the run proceeds on the default. If the fetch **fails** (`ok=False`), do
*not* substitute — attempt `row.model`; a real failure records `error` as today.
Visibility is via the **audit log + dashboard ⚠ badge**, not a Discord ping
(scheduled→Discord notification is the pre-existing gap above and is out of
scope). The scheduler gets `ModelCatalog` injected.

**Interactive runs — fail loud (attended).** The only interactive *trigger*
surface is Discord (the dashboard has no run-trigger endpoint; `/` and
`/settings` are GET-only, and the CLI `ask` path already prints results/errors).
No pre-check; the run proceeds and the LLM call raises for an unknown model.
This design adds **minimal error-surfacing** on the Discord path so the failure
is actually visible: `_on_message`, on a dispatch exception, DMs the user a short
message (e.g. "⚠ couldn't process that — the selected model may be unavailable;
pick another with `/model set`") instead of only logging. No automatic
substitution — the user re-picks. (Scoped narrowly to surfacing run failures on
the Discord path; not a general reliability overhaul.)

**Dashboard ⚠ badge (both).** When rendering `/settings` and `/schedules`, any
selected interactive model or per-schedule model not present in the current
catalog list (`ok=True and model not in models`) shows a "not available" badge,
so stale selections are obvious before they fail. If the catalog fetch fails
(`ok=False`), no badge is shown (we can't assert absence).

**Config-YAML model gone.** Terminal — it *is* the fallback target, so
substitution cannot rescue it. Scheduled fallback resolves to it and still
fails; interactive surfaces the error; the dashboard shows a ⚠ badge on the
default. No code-level recovery is possible.

### Components

**`ModelCatalog`** — `jarvis/agents/model_catalog.py`
- Wraps the `AsyncOpenAI` client; `async list_models() -> list[str]` calls
  `client.models.list()` (the `/v1/models` endpoint) and returns sorted model
  IDs.
- **Fetch lifecycle:** on-demand only, with an in-memory ~30s TTL cache. There
  is no startup fetch and no background poller — the list is queried lazily when
  a UI surface needs it (dashboard render, `/model list`, `/model set`
  autocomplete, and the scheduled-run availability check). The cache prevents a
  burst of autocomplete keystrokes or a page refresh from hammering the
  endpoint; a request past the TTL re-queries. New models therefore appear
  within ~30s of the next picker use, with no restart.
- Returns a small result object distinguishing **success** from **fetch
  failure**: `Catalog(models: list[str], ok: bool)` (or equivalent). `ok=False`
  on endpoint/network error with `models=[]`. This distinction is load-bearing
  for the Hybrid fallback (we only auto-fall-back on a *confirmed* absence,
  i.e. `ok=True and model not in models`, never on `ok=False`).
- Reachable via `AppContext`. To enable this, the `AsyncOpenAI` client built in
  `main.py` is stored on `AppContext.llm_client`.

**`ModelStore`** — `jarvis/agents/model_store.py`
- Backed by `SettingsRepo`, key `llm.active_model`. `None`/absent = "default."
- `async load()` — read the persisted value once at boot into memory.
- `selection() -> str | None` — raw stored value (`None` = default).
- `current() -> str` — resolved; `None` → `config.jarvis.llm.model`.
- `async set(model: str | None)` — `None` clears the override (back to default);
  a string stores it. Writes through to the DB and updates the in-memory value.
- Holds the config default model so `current()` can resolve without a DB read
  on the hot path.

**`ScheduleRow.model`** — new nullable `String` column.
- Alembic migration `0004_schedule_model.py` (`add_column` / `drop_column`).
  Fresh DBs also get it via `Base.metadata.create_all` in bootstrap.
- `ScheduleRepo.create` / `.update` accept `model: str | None`.

**`ScheduledTrigger.model`** — new `model: str | None = None` field
(`jarvis/core/types.py`). The scheduler reads `row.model` and sets it on the
trigger in `_execute_schedule`.

**`AgentRunner`** — constructor takes `model_provider: Callable[[], str]`
instead of reading `llm_config.model` directly. The existing `model` ctor param
(test override) is retained as highest priority. `llm_config` stays for any
non-model use.

### Wiring (`main.py`)

1. Build `AsyncOpenAI` client → store on `AppContext.llm_client`.
2. Create `ModelCatalog(client)` → `AppContext.model_catalog`.
3. Create `ModelStore(settings_repo factory, default=cfg.jarvis.llm.model)`,
   `await store.load()` → `AppContext.model_store`.
4. Interactive `AgentRunner(model_provider=store.current, ...)`.
5. `Scheduler(...)` builds its runner with
   `model_provider=lambda: cfg.jarvis.llm.model` and also receives
   `model_catalog` for the scheduled availability pre-check.
6. `DiscordAdapter` receives injected callables (see below).

## Dashboard

**`/settings`** (`jarvis/web/routes/settings.py`, `settings.html`)
- Replace the read-only "LLM model" row with a form (`POST /settings/model`)
  containing a `<select>`:
  - first option: **"Default (from config: `<model>`)"** (value empty → clears
    override),
  - then live model IDs from `ModelCatalog`.
  - current selection preselected.
- Show YAML default vs. active override (e.g. "Active: `gpt-4o` (override)").
- If the catalog fails, render a free-text input fallback + a notice.
- `POST /settings/model` calls `model_store.set(value or None)`, emits a
  `MODEL_CHANGED` audit event, redirects back (303).

**`/schedules`** (`jarvis/web/routes/schedules.py`, `schedules.html`)
- Add a model `<select>` to the create form (first option "Use default model"
  → empty/`NULL`), populated from `ModelCatalog`.
- `POST /schedules` accepts `model: str = Form("")`, passes `model or None` to
  `ScheduleRepo.create`.
- Add a "Model" column to the existing-schedules table showing `model` or
  "default".

## Discord `/model` slash command

`DiscordAdapter` gains a `discord.app_commands.CommandTree`:
- Built in `_build_client` and **re-attached on every client rebuild**
  (the supervisor recreates the client on reconnect).
- Synced to Discord in `on_ready` (global commands; the app must be installed
  with DM context enabled — a README setup note).

Subcommand group `model`:
- `/model current` — show the active interactive model and whether it's an
  override or the config default.
- `/model list` — list available models from the catalog.
- `/model set <name>` — set the interactive model; `<name>` has **autocomplete**
  sourced from the catalog. A "default" sentinel choice clears the override.

Every callback enforces the existing `allowed_user_ids` gate (reject others).

**Injected dependencies** (keep the adapter decoupled and the logic testable):
- `list_models: Callable[[], Awaitable[list[str]]]`
- `get_active_model: Callable[[], tuple[str, bool]]` — `(model, is_override)`
- `set_active_model: Callable[[str | None], Awaitable[None]]`

These are wired in `main.py` to `ModelCatalog` / `ModelStore`. They are
optional; when absent (e.g. minimal test setups) the command is not registered.

The command **handler logic** (gate check, current/list/set behavior) is
extracted into plain async functions taking the injected callables, so it is
unit-testable without a live gateway.

## Audit

- New `AuditEventType.MODEL_CHANGED = "model.changed"`. Emitted on every
  interactive change (dashboard or Discord) with payload `{old, new, source}`
  where `source ∈ {"dashboard", "discord"}`.
- New `AuditEventType.MODEL_FALLBACK = "model.fallback"`. Emitted when a
  scheduled run's pinned model is confirmed absent and the config default is
  substituted, with payload `{schedule_id, requested, substituted}`.
- The interactive model-unavailable failure reuses the existing LLM-error audit
  path plus the new user-facing reply; no extra event type is required for it.

## Error handling

- `/v1/models` failure → `Catalog(models=[], ok=False)`; UIs degrade to manual
  text entry, never crash a page or command, and no ⚠ badges or auto-fallback
  are triggered (we can't assert absence).
- `model_store.set` with an unknown model string is allowed (the endpoint may
  expose models the catalog cache hasn't refreshed). The selection is honored;
  staleness is handled per the Hybrid section.
- Stale selected model: scheduled → auto-fall-back to config default (audit +
  dashboard badge); interactive → fail loud with a user-facing Discord DM reply.
  See the Hybrid section.
- Discord command from a non-allow-listed user → silently rejected (consistent
  with `on_message`).

## Testing

- `ModelCatalog`: mocked `client.models.list` returns IDs (sorted); error path
  returns `Catalog(models=[], ok=False)`; cache TTL behavior (no re-query within
  TTL, re-query after).
- `ModelStore`: `current()` fallback to config default when unset; `set(x)` then
  `current()`; `set(None)` clears; persistence via in-memory SQLite + reload.
- `AgentRunner` resolution precedence: ctor override > scheduled-trigger model >
  `model_provider()`; interactive trigger always uses provider.
- `Scheduler`: `_execute_schedule` carries `row.model` onto `ScheduledTrigger`;
  `NULL` → provider (config default); pinned-model **auto-fallback** when
  catalog `ok=True and model absent` (substitutes default + `MODEL_FALLBACK`
  audit); **no** fallback when catalog `ok=False`.
- Interactive fail-loud: `_on_message` dispatch exception → user-facing DM reply
  sent (Discord path only).
- Dashboard ⚠ badge: rendered when `ok=True and model not in models`; absent
  when `ok=False`.
- Routes: `POST /settings/model` updates the store + emits audit; `POST
  /schedules` with/without `model`.
- Discord: extracted handler functions — allow-list gate, `current`, `list`,
  `set`, and the "default" sentinel clearing the override.
- Migration `0004` upgrade/downgrade smoke (column present/absent).

## Out of scope

- Bot/agent-initiated schedule creation (no such tool exists today).
- Per-channel or per-conversation interactive models (single global interactive
  selection only).
- Writing selections back to YAML.
- Validating that a selected model exists before saving (endpoints vary; we
  surface staleness per the Hybrid section instead).
- Fully wiring scheduled→Discord notifications (the empty `discord_user_id` /
  unused `notify_on_error` gap). The scheduled fallback is surfaced via the
  audit log + dashboard badge, not a Discord ping. Fixing scheduled DM delivery
  is a separate concern.
- A general interactive-reliability overhaul. The interactive error-surfacing
  added here is scoped to making run failures (notably model-unavailable)
  visible to the user; broader retry/queueing is not addressed.

## Files touched

New:
- `jarvis/agents/model_catalog.py`
- `jarvis/agents/model_store.py`
- `alembic/versions/0004_schedule_model.py`

Modified:
- `jarvis/agents/runner.py` (model_provider)
- `jarvis/core/types.py` (`ScheduledTrigger.model`, `AuditEventType.MODEL_CHANGED`,
  `AuditEventType.MODEL_FALLBACK`)
- `jarvis/persistence/models.py` (`ScheduleRow.model`)
- `jarvis/persistence/repositories.py` (`ScheduleRepo.create/.update`)
- `jarvis/scheduler/scheduler.py` (carry `row.model`; provider wiring; catalog
  pre-check + auto-fallback + `MODEL_FALLBACK` audit)
- `jarvis/main.py` (store client, build catalog + store, wire providers, catalog,
  and adapter deps)
- `jarvis/channels/discord_adapter.py` (`CommandTree`, `/model`, injected deps;
  user-facing error reply on dispatch failure)
- `jarvis/web/routes/settings.py` (`POST /settings/model`; ⚠ badge data)
- `jarvis/web/routes/schedules.py` (`model` form field; ⚠ badge data)
- `jarvis/web/templates/settings.html`, `schedules.html` (model selects, ⚠ badges)
- `README.md` (Discord app DM-context note; model selection usage)

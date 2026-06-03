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

### Components

**`ModelCatalog`** — `jarvis/agents/model_catalog.py`
- Wraps the `AsyncOpenAI` client; `async list_models() -> list[str]` calls
  `client.models.list()` (the `/v1/models` endpoint) and returns sorted model
  IDs.
- In-memory TTL cache (~30s) so dashboard loads and Discord autocomplete
  keystrokes don't hammer the endpoint.
- Errors (endpoint lacks `/v1/models`, network failure) are caught and reported
  as an empty list plus an `ok: bool` / error flag so callers can show
  "couldn't load models — type a name manually."
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
5. `Scheduler(... )` builds its runner with
   `model_provider=lambda: cfg.jarvis.llm.model`.
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

New `AuditEventType.MODEL_CHANGED = "model.changed"`. Emitted on every
interactive change (dashboard or Discord) with payload
`{old, new, source}` where `source ∈ {"dashboard", "discord"}`.

## Error handling

- `/v1/models` failure → empty list + flag; UIs degrade to manual text entry,
  never crash a page or command.
- `model_store.set` with an unknown model string is allowed (the endpoint may
  expose models the catalog cache hasn't refreshed). The selection is honored;
  a bad model surfaces as a normal run-time LLM error on the next run.
- Discord command from a non-allow-listed user → silently rejected (consistent
  with `on_message`).

## Testing

- `ModelCatalog`: mocked `client.models.list` returns IDs (sorted); error path
  returns empty + flag; cache TTL behavior.
- `ModelStore`: `current()` fallback to config default when unset; `set(x)` then
  `current()`; `set(None)` clears; persistence via in-memory SQLite + reload.
- `AgentRunner` resolution precedence: ctor override > scheduled-trigger model >
  `model_provider()`; interactive trigger always uses provider.
- `Scheduler`: `_execute_schedule` carries `row.model` onto `ScheduledTrigger`;
  `NULL` → provider (config default).
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
  surface failures at run time).

## Files touched

New:
- `jarvis/agents/model_catalog.py`
- `jarvis/agents/model_store.py`
- `alembic/versions/0004_schedule_model.py`

Modified:
- `jarvis/agents/runner.py` (model_provider)
- `jarvis/core/types.py` (`ScheduledTrigger.model`, `AuditEventType.MODEL_CHANGED`)
- `jarvis/persistence/models.py` (`ScheduleRow.model`)
- `jarvis/persistence/repositories.py` (`ScheduleRepo.create/.update`)
- `jarvis/scheduler/scheduler.py` (carry `row.model`; provider wiring)
- `jarvis/main.py` (store client, build catalog + store, wire providers + adapter)
- `jarvis/channels/discord_adapter.py` (`CommandTree`, `/model`, injected deps)
- `jarvis/web/routes/settings.py` (`POST /settings/model`)
- `jarvis/web/routes/schedules.py` (`model` form field)
- `jarvis/web/templates/settings.html`, `schedules.html`
- `README.md` (Discord app DM-context note; model selection usage)

# Daily Brief / Digest Templates — Design

**Date:** 2026-06-04
**Status:** Draft for user review

## Goal

Add reusable Daily Brief / Digest templates so Jarvis can turn common scheduled
workflows into repeatable schedule creation patterns without making existing
schedules depend on mutable shared prompts.

The v1 goal is practical:

- ship useful built-in templates immediately;
- let the operator create, edit, and clone templates from the dashboard;
- let a schedule be created from a template;
- snapshot template fields into the schedule at creation time;
- keep existing schedule execution unchanged.

## Background

Jarvis already has the runtime pieces this feature needs:

- `schedules` store a durable name, description, cron expression, timezone,
  prompt, output mode, model, Discord recipient, enabled flag, and run metadata.
- `/schedules` exposes a create form and list table with Run now, enable/disable,
  and delete actions.
- Scheduled execution reads the schedule row directly and dispatches its prompt
  through the existing scheduler/dispatcher/agent path.
- The dashboard style is dense and operational, so template management should
  fit into that existing FastAPI/Jinja2 surface instead of adding a separate
  guided wizard.

The important product decision is that templates are **snapshotted** into
schedules. A schedule created from a template owns its copied prompt and defaults
after creation. Editing a template later does not change existing schedules.

## Decisions

1. **Persist templates as first-class records.** Templates are not hard-coded UI
   strings. They live in a `digest_templates` table and can be edited or cloned.
2. **Seed built-in templates idempotently.** Jarvis ships four built-in records:
   `Daily Brief`, `Email Digest`, `Calendar Brief`, and `Action Inbox Review`.
3. **Snapshot on schedule creation.** Creating a schedule from a template copies
   template fields into the submitted schedule. The schedule does not store a
   live template foreign key for execution.
4. **Keep schedule execution unchanged.** The scheduler continues reading
   `ScheduleRow.prompt`, `cron_expr`, `output_mode`, `model`, and
   `discord_user_id` exactly as it does today.
5. **Protect built-in identity, not content.** Built-in templates have stable
   keys and cannot be disabled, but their operator-facing text/defaults can be
   edited locally. Resetting a built-in template to stock content is outside
   v1.
6. **No variable engine in v1.** Template prompts are plain text. They may
   include natural-language instructions such as "use today's date", but Jarvis
   will not add placeholder substitution or templating syntax in this pass.

## Data Model

Add `digest_templates`:

- `id: UUID`
- `key: string | NULL`, unique when present
- `name: string`
- `description: text`
- `category: string`
- `prompt: text`
- `default_cron_expr: string`
- `default_timezone: string`
- `default_output_mode: string`
- `default_model: string | NULL`
- `default_discord_user_id: string | NULL`
- `built_in: bool`
- `enabled: bool`
- `created_at: datetime`
- `updated_at: datetime`

Indexes:

- `key` unique for seed identity.
- `enabled, category, name` for dashboard listing and schedule-form selectors.

No migration changes are required for `schedules`. Snapshot provenance can be
added later if useful, but v1 does not need it to run or explain schedules.

## Built-In Templates

Seed these templates on startup after migrations, using stable keys:

### Daily Brief

Default cron: `0 8 * * *`

Purpose: produce a morning summary across calendar, email, pending actions, and
anything that looks time-sensitive.

Prompt shape:

- summarize today's calendar;
- flag schedule conflicts and preparation items;
- summarize important unread or recent email if mail tools are available;
- include pending Action Inbox items if available;
- end with a short prioritized action list;
- keep the response concise and suitable for Discord.

### Email Digest

Default cron: `0 9 * * 1-5`

Purpose: summarize important email activity without requiring a full daily
brief.

Prompt shape:

- review recent unread and important messages;
- group by sender or topic;
- identify messages needing a reply;
- call out receipts, travel, bills, or operational alerts;
- avoid listing low-value notification noise.

### Calendar Brief

Default cron: `30 7 * * *`

Purpose: focus only on schedule awareness and meeting preparation.

Prompt shape:

- summarize today's events;
- identify preparation tasks, travel buffers, and conflicts;
- note tomorrow morning's first commitment when useful;
- keep it short.

### Action Inbox Review

Default cron: `0 16 * * 1-5`

Purpose: remind the operator about pending approvals and unresolved agent work.

Prompt shape:

- review pending Action Inbox items if available;
- summarize what each pending action is waiting on;
- group stale or risky items first;
- suggest approve/reject follow-up where context is clear;
- stay read-only unless an action is explicitly approved through the existing
  Action Inbox flow.

All built-in templates default to `discord` output mode, `UTC` timezone, and no
pinned model or Discord recipient. The schedule form may still default the
Discord recipient from the configured single allowed Discord user, matching
current schedule behavior.

## Dashboard

Add a top-level nav link: **Templates**.

`GET /templates`:

- list enabled templates grouped or sorted by category/name;
- show built-in status, default cron, output mode, and a prompt preview;
- provide actions to edit, clone, disable, or create a schedule from a template.

`GET /templates/new`:

- render a create form for user-defined templates.

`POST /templates`:

- validate required fields and create a user template.

`GET /templates/{id}`:

- render the edit form and full prompt.

`POST /templates/{id}`:

- update editable fields.

`POST /templates/{id}/clone`:

- create a user-owned copy with `built_in=false` and no `key`.

`POST /templates/{id}/disable`:

- disable a user template.
- built-in templates cannot be disabled in v1; if a built-in is not useful, the
  operator can edit it or clone a replacement.

Schedule creation integration:

- `/schedules` shows a template selector above the existing prompt fields.
- Selecting a template fills the create form with template defaults when the
  page is rendered for that template.
- `POST /schedules` remains the source of truth for schedule creation. It
  receives ordinary form fields and persists a normal `ScheduleRow`.
- A "Create schedule" action from `/templates` links to
  `/schedules?template_id=<id>` so the operator can review and edit before
  saving.

The UI should remain compact and table-driven. This is an operations dashboard,
not a marketing or onboarding flow.

## Services and Boundaries

Add a `DigestTemplateRepo` for persistence operations:

- `list_enabled()`
- `list_all()`
- `get(template_id)`
- `get_by_key(key)`
- `create(...)`
- `update(...)`
- `clone(template_id)`
- `seed_built_ins()`

Add a small template seed module, for example
`jarvis/scheduler/digest_templates.py` or `jarvis/templates/seeds.py`, that owns
the seed definitions and idempotent seed logic. The seed operation should:

- create a missing built-in by `key`;
- leave locally edited built-ins unchanged if the key already exists;
- not create duplicates across restarts.

Route handlers stay thin and call the repository. No scheduler or agent changes
are required.

## Data Flow

Creating a schedule from a template:

1. Operator opens `/templates` and clicks "Create schedule" for a template.
2. Browser navigates to `/schedules?template_id=<id>`.
3. `schedule_list` loads the selected template and renders the create form with
   its defaults.
4. Operator edits any fields.
5. `POST /schedules` creates a normal schedule row.
6. Future runs execute from the schedule snapshot, independent of the template.

Editing a template:

1. Operator edits the template from `/templates/{id}`.
2. The template row is updated.
3. Existing schedules are untouched.
4. New schedules created from the template use the new defaults.

Startup seeding:

1. App startup completes database setup/migrations as today.
2. Jarvis runs `seed_built_ins()`.
3. Missing seed keys are inserted.
4. Existing keys are left as-is.

## Error Handling

- Invalid template IDs on `/schedules?template_id=...` render the schedule form
  without template defaults and show a concise warning.
- Attempts to disable built-in templates are rejected with a clear dashboard
  message.
- Template create/update validates non-empty name, prompt, cron expression,
  timezone, and output mode.
- Cron parsing remains enforced by scheduler registration and existing schedule
  behavior. The route can perform lightweight validation before insert if the
  current schedule create path already does so during implementation.
- Seed failures should be logged and surfaced in audit or startup logs, but they
  should not prevent Jarvis from starting unless the database itself is broken.

## Testing

Use the repo's test-first pattern.

Focused tests:

- migration round-trip creates/drops `digest_templates`;
- ORM row round-trip for `DigestTemplateRow`;
- repository create/update/clone/list behavior;
- built-in seed is idempotent and does not overwrite local edits;
- `/templates` lists seed templates;
- template edit and clone routes persist expected rows;
- `/schedules?template_id=<id>` renders template defaults in the schedule form;
- creating a schedule from rendered defaults persists a normal `ScheduleRow`;
- existing scheduler tests remain unchanged because execution uses schedule
  snapshots.

Final verification:

- `uv run ruff check jarvis tests`
- `uv run pytest -vv --durations=20`
- browser check of `/templates` and `/schedules?template_id=<id>` if a local
  server is started.

## Non-Goals

- live-linked template execution;
- placeholder substitution or a template rendering language;
- per-template run history;
- automatic mutation of existing schedules when templates change;
- Discord commands for template management;
- import/export of template packs.

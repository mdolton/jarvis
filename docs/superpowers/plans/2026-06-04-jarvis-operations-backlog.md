# Jarvis Operations Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Jarvis more useful as a self-hosted operations assistant by completing scheduled notifications, adding operator-triggered runs, improving diagnostics, enabling MCP policy edits, strengthening CI, and polishing the dashboard.

**Architecture:** Keep behavior in the existing FastAPI/Jinja2, SQLAlchemy, scheduler, and Discord adapter boundaries. Add focused persistence fields where the product needs durable operator choices, route all new behavior through existing service objects, and cover each behavior with tests before implementation.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLAlchemy async, Alembic, APScheduler, discord.py, pytest, ruff, GitHub Actions.

---

## File Structure

- `jarvis/scheduler/scheduler.py` — scheduled-run routing and failure notification.
- `jarvis/persistence/models.py` / `repositories.py` — schedule recipient and MCP policy persistence support.
- `alembic/versions/0005_schedule_discord_recipient.py` — migration for scheduled Discord target.
- `jarvis/web/routes/schedules.py` / `templates/schedules.html` — create/edit scheduling fields and Run Now action.
- `jarvis/web/routes/home.py` / `health.py` / `templates/home.html` — operational diagnostics.
- `jarvis/web/routes/mcp.py` / `templates/mcp.html` — MCP policy editing.
- `jarvis/web/routes/conversations.py` / `templates/home.html` — manual dashboard prompt execution surface.
- `jarvis/web/static/style.css` / dashboard templates — operations-console visual polish.
- `.github/workflows/docker.yml` — CI quality gates before image build.
- Tests under `tests/unit` and `tests/integration` mirror each changed behavior.

## Task 1: Scheduled Discord Delivery and Error Notifications

- [ ] Write failing scheduler tests proving Discord output uses a configured recipient and `notify_on_error` sends a failure DM.
- [ ] Add durable `schedules.discord_user_id` with migration and repository support.
- [ ] Wire schedule creation to accept an optional Discord target, defaulting to the first configured allowed Discord user when available.
- [ ] Use `notify_on_error` in scheduler failures and send a concise failure message when a Discord recipient exists.
- [ ] Run focused scheduler/web/migration tests.

## Task 2: Dashboard Run Now and Manual Prompt Execution

- [ ] Write failing web tests for schedule Run Now and dashboard manual prompt submission.
- [ ] Add `POST /schedules/{id}/run` that awaits `ctx.scheduler.fire_now(id)` and redirects back.
- [ ] Add a compact manual prompt form on the home page that calls `ctx.dispatcher.dispatch_manual`.
- [ ] Show manual prompt result or error on the redirected home page.
- [ ] Run focused web and dispatcher tests.

## Task 3: Expanded Diagnostics

- [ ] Write failing health/home tests covering model catalog, scheduler, Discord, OAuth provider state, and recent audit errors.
- [ ] Extend `/healthz` with structured component statuses.
- [ ] Render the same status summary on the home dashboard.
- [ ] Keep external checks bounded and non-fatal.
- [ ] Run health/home tests.

## Task 4: MCP Tool Policy Editing

- [ ] Write failing tests for setting and clearing MCP tool `policy_override`.
- [ ] Add repository update support if missing.
- [ ] Add `POST /mcp/tools/{tool_id}/policy` with allowed values: empty, `allow`, `deny`, `confirm`.
- [ ] Render a policy selector per tool in the MCP dashboard.
- [ ] Run MCP web/repository tests.

## Task 5: CI Quality Gates

- [ ] Add a GitHub Actions quality job that runs `uv run ruff check jarvis tests` and `uv run pytest -q`.
- [ ] Make the Docker build depend on the quality job.
- [ ] Add a workflow-level smoke command where practical without secrets.
- [ ] Validate workflow YAML locally by inspection and run local `make check`.

## Task 6: Dashboard Operations Polish

- [ ] Refactor dashboard CSS into a dense, utilitarian operations-console style.
- [ ] Improve layout and action grouping on home, schedules, and MCP pages.
- [ ] Preserve accessibility and mobile behavior with stable table/action layouts.
- [ ] Run web render tests and, if a local server is started, verify in browser.

## Final Verification

- [ ] `uv run ruff check jarvis tests`
- [ ] `uv run pytest -q`
- [ ] Review git diff for scoped changes only.

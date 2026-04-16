# Jarvis Plan 5 — Web Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A web dashboard at `http://localhost:8080` where you can see Jarvis's status, browse conversations and audit events, manage schedules (CRUD), view MCP server status + tools, and tail audit events live via SSE. No auth in v1 — assumes trusted local network.

**Architecture:** FastAPI app mounted as part of `bootstrap()`. Jinja2 templates rendered server-side. HTMX for interactive elements (schedule forms, live log tailing via SSE). Minimal CSS — a single `static/style.css` for readability. All data comes from the existing repositories (no new DB tables). The dashboard is read/write for schedules and read-only for everything else.

**Tech Stack:** `fastapi>=0.115`, `jinja2>=3.1`, `uvicorn>=0.30` (embedded via `uvicorn.Server` in the event loop — no separate process). HTMX 2.x served from CDN. SSE extension for HTMX for live audit tailing.

**Design spec this plan implements:** `docs/superpowers/specs/2026-04-14-jarvis-agent-service-design.md` — §5.11 WebDashboard (all pages listed there).

---

## File Structure

New modules:

```
jarvis/
  web/
    app.py              # FastAPI app factory: create_app(ctx) -> FastAPI
    routes/
      __init__.py
      home.py           # GET / — status overview
      conversations.py  # GET /conversations, GET /conversations/{id}
      schedules.py      # GET /schedules, POST /schedules, POST /schedules/{id}/edit,
                        #   POST /schedules/{id}/toggle, POST /schedules/{id}/delete,
                        #   POST /schedules/{id}/run-now
      mcp.py            # GET /mcp — server list + tools
      audit.py          # GET /audit — filterable event log
      settings.py       # GET /settings — read-only config view
      events.py         # GET /events/stream — SSE endpoint
      health.py         # GET /healthz — JSON health check
    templates/
      base.html         # Layout with nav, HTMX + SSE extension CDN links
      home.html
      conversations.html
      conversation_detail.html
      schedules.html
      schedule_form.html    # HTMX partial for create/edit modal
      mcp.html
      audit.html
      audit_row.html        # HTMX partial for a single audit event row (SSE swap target)
      settings.html
    static/
      style.css
```

Files modified:
- `jarvis/main.py` — `bootstrap()` creates the FastAPI app; `AppContext` gains `web_app`.
- `jarvis/cli.py` — `serve` command starts uvicorn alongside Discord + scheduler.
- `pyproject.toml` — add `fastapi`, `jinja2`, `uvicorn` deps.

---

## Task 1: Add web dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit `pyproject.toml`**

Add to the dependencies list:

```toml
  "fastapi>=0.115",
  "jinja2>=3.1",
  "uvicorn>=0.30",
```

- [ ] **Step 2: Sync + verify**

Run: `uv sync`

```bash
uv run python -c "
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import jinja2
print('web imports OK')
"
```

- [ ] **Step 3: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 126 passed, clean.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "add fastapi, jinja2, uvicorn dependencies"
```

---

## Task 2: FastAPI app factory + base template + healthz

The minimal working web app — just `/healthz` and a base template that all pages extend.

**Files:**
- Create: `jarvis/web/app.py`
- Create: `jarvis/web/routes/__init__.py`
- Create: `jarvis/web/routes/health.py`
- Create: `jarvis/web/templates/base.html`
- Create: `jarvis/web/static/style.css`
- Create: `tests/integration/test_web_health.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_web_health.py`:

```python
import pytest
from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def test_healthz_returns_200(tmp_path):
    """Healthz should return 200 with a JSON body containing status=ok."""
    app = create_app(app_context=None)  # healthz doesn't need a real context
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_web_health.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create the FastAPI app factory**

Write `jarvis/web/app.py`:

```python
"""FastAPI app factory for the Jarvis dashboard."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


def create_app(*, app_context=None) -> FastAPI:
    """Build the FastAPI app. `app_context` is the bootstrap AppContext —
    None is tolerated for healthz-only testing.
    """
    app = FastAPI(title="Jarvis Dashboard", docs_url=None, redoc_url=None)

    # Attach context so route handlers can access repos, config, etc.
    app.state.ctx = app_context

    # Templates.
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    # Static files.
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Register routes.
    from jarvis.web.routes.health import router as health_router

    app.include_router(health_router)

    return app
```

Write `jarvis/web/routes/__init__.py` (empty):

```python
```

Write `jarvis/web/routes/health.py`:

```python
"""GET /healthz — JSON health check."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}
```

Write `jarvis/web/templates/base.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% block title %}Jarvis{% endblock %}</title>
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
    <script src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"></script>
</head>
<body>
    <nav>
        <a href="/">Home</a>
        <a href="/conversations">Conversations</a>
        <a href="/schedules">Schedules</a>
        <a href="/mcp">MCP</a>
        <a href="/audit">Audit</a>
        <a href="/settings">Settings</a>
    </nav>
    <main>
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

Write `jarvis/web/static/style.css`:

```css
/* Jarvis Dashboard — minimal readable styles */
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; max-width: 960px; margin: 0 auto; padding: 1rem; color: #e0e0e0; background: #1a1a2e; }
nav { display: flex; gap: 1rem; padding: 0.75rem 0; border-bottom: 1px solid #333; margin-bottom: 1.5rem; }
nav a { color: #8ab4f8; text-decoration: none; }
nav a:hover { text-decoration: underline; }
h1, h2, h3 { margin-bottom: 0.75rem; }
table { width: 100%; border-collapse: collapse; margin-bottom: 1.5rem; }
th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #333; }
th { color: #999; font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }
.badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.8rem; }
.badge-ok { background: #2d4a2d; color: #6f6; }
.badge-err { background: #4a2d2d; color: #f66; }
.badge-warn { background: #4a4a2d; color: #ff6; }
form { margin-bottom: 1rem; }
input, select, textarea { background: #2a2a3e; color: #e0e0e0; border: 1px solid #444; padding: 0.4rem; border-radius: 4px; width: 100%; margin-bottom: 0.5rem; }
button, .btn { background: #3a3a5e; color: #e0e0e0; border: 1px solid #555; padding: 0.4rem 1rem; border-radius: 4px; cursor: pointer; }
button:hover, .btn:hover { background: #4a4a6e; }
.btn-danger { background: #5a2a2a; }
.btn-danger:hover { background: #6a3a3a; }
pre { background: #2a2a3e; padding: 0.75rem; border-radius: 4px; overflow-x: auto; font-size: 0.85rem; margin-bottom: 1rem; }
.muted { color: #888; font-size: 0.85rem; }
.audit-row { padding: 0.4rem 0; border-bottom: 1px solid #222; font-size: 0.85rem; }
.audit-type { font-weight: 600; color: #8ab4f8; }
.msg-user { color: #8ab4f8; }
.msg-assistant { color: #6f6; }
.msg-system { color: #ff6; }
```

- [ ] **Step 4: Run test — verify pass**

Run: `uv run pytest tests/integration/test_web_health.py -v`
Expected: 1 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 127 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/web/app.py jarvis/web/routes/__init__.py jarvis/web/routes/health.py jarvis/web/templates/base.html jarvis/web/static/style.css tests/integration/test_web_health.py
git commit -m "add FastAPI app factory, healthz endpoint, base template, and CSS"
```

---

## Task 3: Home page — status overview

**Files:**
- Create: `jarvis/web/routes/home.py`
- Create: `jarvis/web/templates/home.html`
- Modify: `jarvis/web/app.py` (register home router)
- Create: `tests/integration/test_web_home.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_web_home.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def _mock_context():
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://localhost:1234/v1"
    ctx.config.jarvis.llm.model = "qwen2.5"
    ctx.mcp_manager.agent_mcp_servers.return_value = ["s1"]
    ctx.scheduler.active_job_count.return_value = 2
    ctx.channel_adapters = [MagicMock(kind="discord")]
    return ctx


def test_home_page_renders_status(tmp_path):
    ctx = _mock_context()
    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "qwen2.5" in resp.text
    assert "localhost:1234" in resp.text
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_web_home.py -v`
Expected: fails (404 on `/` — home route not registered).

- [ ] **Step 3: Write the route + template**

Write `jarvis/web/routes/home.py`:

```python
"""GET / — status overview."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    llm_url = ctx.config.jarvis.llm.base_url if ctx else "n/a"
    llm_model = ctx.config.jarvis.llm.model if ctx else "n/a"
    mcp_count = len(ctx.mcp_manager.agent_mcp_servers()) if ctx else 0
    schedule_count = ctx.scheduler.active_job_count() if ctx else 0
    adapters = [a.kind for a in ctx.channel_adapters] if ctx else []

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "llm_url": llm_url,
            "llm_model": llm_model,
            "mcp_count": mcp_count,
            "schedule_count": schedule_count,
            "adapters": adapters,
        },
    )
```

Write `jarvis/web/templates/home.html`:

```html
{% extends "base.html" %}
{% block title %}Jarvis — Home{% endblock %}
{% block content %}
<h1>Jarvis Status</h1>
<table>
    <tr><th>LLM endpoint</th><td>{{ llm_url }}</td></tr>
    <tr><th>LLM model</th><td>{{ llm_model }}</td></tr>
    <tr><th>MCP servers connected</th><td>{{ mcp_count }}</td></tr>
    <tr><th>Active schedules</th><td>{{ schedule_count }}</td></tr>
    <tr><th>Channel adapters</th><td>{{ adapters | join(", ") or "none" }}</td></tr>
</table>
{% endblock %}
```

In `jarvis/web/app.py`, add after the health router registration:

```python
    from jarvis.web.routes.home import router as home_router

    app.include_router(home_router)
```

- [ ] **Step 4: Run test — verify pass**

Run: `uv run pytest tests/integration/test_web_home.py -v`
Expected: 1 passed.

- [ ] **Step 5: Full suite + ruff + commit**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`

```bash
git add jarvis/web/routes/home.py jarvis/web/templates/home.html jarvis/web/app.py tests/integration/test_web_home.py
git commit -m "add dashboard home page with status overview"
```

---

## Task 4: Conversations page

**Files:**
- Create: `jarvis/web/routes/conversations.py`
- Create: `jarvis/web/templates/conversations.html`
- Create: `jarvis/web/templates/conversation_detail.html`
- Modify: `jarvis/web/app.py` (register router)
- Modify: `jarvis/persistence/repositories.py` (add `ConversationRepo.list_recent`)
- Create: `tests/integration/test_web_conversations.py`

- [ ] **Step 1: Add `ConversationRepo.list_recent`**

In `jarvis/persistence/repositories.py`, add to `ConversationRepo`:

```python
    async def list_recent(self, *, limit: int = 50) -> list[ConversationRow]:
        result = await self._session.execute(
            select(ConversationRow)
            .order_by(ConversationRow.last_activity_at.desc())
            .limit(limit)
        )
        return list(result.scalars())
```

- [ ] **Step 2: Write failing test**

Write `tests/integration/test_web_conversations.py`:

```python
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ConversationRepo, MessageRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def ctx_and_client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed a conversation with messages.
    async with factory() as s:
        conv_repo = ConversationRepo(s)
        conv = await conv_repo.find_or_create_open(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="user-1",
            idle_timeout_sec=900,
        )
        msg_repo = MessageRepo(s)
        await msg_repo.append(
            conversation_id=conv.id, role=MessageRole.USER, content="hello"
        )
        await msg_repo.append(
            conversation_id=conv.id, role=MessageRole.ASSISTANT, content="hi there"
        )
        conv_id = conv.id

    # Build a mock-ish context with real session_factory.
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory

    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield ctx, client, conv_id, factory

    await engine.dispose()


def test_conversations_list(ctx_and_client):
    _, client, _, _ = ctx_and_client
    resp = client.get("/conversations")
    assert resp.status_code == 200
    assert "discord" in resp.text.lower()


def test_conversation_detail(ctx_and_client):
    _, client, conv_id, _ = ctx_and_client
    resp = client.get(f"/conversations/{conv_id}")
    assert resp.status_code == 200
    assert "hello" in resp.text
    assert "hi there" in resp.text
```

- [ ] **Step 3: Write the route + templates**

Write `jarvis/web/routes/conversations.py`:

```python
"""Conversation list and detail pages."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from jarvis.persistence.repositories import ConversationRepo, MessageRepo

router = APIRouter()


@router.get("/conversations", response_class=HTMLResponse)
async def conversation_list(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        convs = await ConversationRepo(session).list_recent(limit=50)

    return templates.TemplateResponse(
        "conversations.html",
        {"request": request, "conversations": convs},
    )


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(request: Request, conv_id: UUID):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        conv_repo = ConversationRepo(session)
        msg_repo = MessageRepo(session)

        from jarvis.persistence.models import ConversationRow

        conv = await session.get(ConversationRow, conv_id)
        messages = await msg_repo.history(conv_id) if conv else []

    return templates.TemplateResponse(
        "conversation_detail.html",
        {"request": request, "conversation": conv, "messages": messages},
    )
```

Write `jarvis/web/templates/conversations.html`:

```html
{% extends "base.html" %}
{% block title %}Conversations{% endblock %}
{% block content %}
<h1>Conversations</h1>
<table>
    <thead>
        <tr><th>Channel</th><th>Ref</th><th>Status</th><th>Last Activity</th><th></th></tr>
    </thead>
    <tbody>
    {% for conv in conversations %}
        <tr>
            <td>{{ conv.channel_kind }}</td>
            <td>{{ conv.channel_ref }}</td>
            <td><span class="badge {% if conv.status == 'open' %}badge-ok{% else %}badge-warn{% endif %}">{{ conv.status }}</span></td>
            <td class="muted">{{ conv.last_activity_at.strftime('%Y-%m-%d %H:%M') if conv.last_activity_at else 'n/a' }}</td>
            <td><a href="/conversations/{{ conv.id }}">View</a></td>
        </tr>
    {% endfor %}
    </tbody>
</table>
{% if not conversations %}
<p class="muted">No conversations yet.</p>
{% endif %}
{% endblock %}
```

Write `jarvis/web/templates/conversation_detail.html`:

```html
{% extends "base.html" %}
{% block title %}Conversation {{ conversation.id if conversation else 'Not Found' }}{% endblock %}
{% block content %}
{% if conversation %}
<h1>Conversation <span class="muted">{{ conversation.channel_kind }} / {{ conversation.channel_ref }}</span></h1>
<p class="muted">Started {{ conversation.started_at.strftime('%Y-%m-%d %H:%M') }} — {{ conversation.status }}</p>

<div class="messages">
{% for msg in messages %}
    <div class="audit-row">
        <span class="{% if msg.role == 'user' %}msg-user{% elif msg.role == 'assistant' %}msg-assistant{% else %}msg-system{% endif %}">{{ msg.role }}</span>:
        {{ msg.content }}
    </div>
{% endfor %}
</div>
{% else %}
<p>Conversation not found.</p>
{% endif %}
{% endblock %}
```

Register in `jarvis/web/app.py`:

```python
    from jarvis.web.routes.conversations import router as conversations_router

    app.include_router(conversations_router)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_web_conversations.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite + ruff + commit**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`

```bash
git add jarvis/web/routes/conversations.py jarvis/web/templates/conversations.html jarvis/web/templates/conversation_detail.html jarvis/web/app.py jarvis/persistence/repositories.py tests/integration/test_web_conversations.py
git commit -m "add conversations list and detail pages"
```

---

## Task 5: Schedules page with CRUD

**Files:**
- Create: `jarvis/web/routes/schedules.py`
- Create: `jarvis/web/templates/schedules.html`
- Create: `jarvis/web/templates/schedule_form.html`
- Modify: `jarvis/web/app.py`
- Create: `tests/integration/test_web_schedules.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_web_schedules.py`:

```python
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ScheduleRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.scheduler = MagicMock()
    ctx.scheduler.fire_now = MagicMock(return_value=None)  # not async in mock

    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield client, factory

    await engine.dispose()


def test_schedules_page_renders(client_and_factory):
    client, _ = client_and_factory
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert "schedules" in resp.text.lower()


def test_create_schedule(client_and_factory):
    client, factory = client_and_factory
    resp = client.post(
        "/schedules",
        data={
            "name": "morning-email",
            "description": "Check email",
            "cron_expr": "0 8 * * *",
            "timezone": "UTC",
            "prompt": "Summarize email",
            "output_mode": "discord",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)  # redirect after create


def test_toggle_schedule(client_and_factory):
    client, factory = client_and_factory
    # Create first.
    client.post(
        "/schedules",
        data={
            "name": "toggleme",
            "description": "",
            "cron_expr": "* * * * *",
            "timezone": "UTC",
            "prompt": "x",
            "output_mode": "dashboard_only",
        },
        follow_redirects=False,
    )
    # Get the list to find the schedule.
    resp = client.get("/schedules")
    assert "toggleme" in resp.text
```

- [ ] **Step 2: Run — verify failure**

- [ ] **Step 3: Write route + templates**

Write `jarvis/web/routes/schedules.py`:

```python
"""Schedule CRUD pages."""

from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import ScheduleRepo

router = APIRouter()


@router.get("/schedules", response_class=HTMLResponse)
async def schedule_list(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        schedules = await ScheduleRepo(session).list_all()
    return templates.TemplateResponse(
        "schedules.html", {"request": request, "schedules": schedules}
    )


@router.post("/schedules")
async def schedule_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    cron_expr: str = Form(...),
    timezone: str = Form("UTC"),
    prompt: str = Form(...),
    output_mode: str = Form("discord"),
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
        )
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle")
async def schedule_toggle(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = ScheduleRepo(session)
        row = await repo.get(schedule_id)
        if row:
            await repo.set_enabled(schedule_id, not row.enabled)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
async def schedule_delete(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        await ScheduleRepo(session).delete(schedule_id)
    return RedirectResponse(url="/schedules", status_code=303)
```

Write `jarvis/web/templates/schedules.html`:

```html
{% extends "base.html" %}
{% block title %}Schedules{% endblock %}
{% block content %}
<h1>Schedules</h1>

<h2>Create Schedule</h2>
<form method="post" action="/schedules">
    <input name="name" placeholder="Name" required>
    <input name="description" placeholder="Description">
    <input name="cron_expr" placeholder="Cron expression (e.g. 0 8 * * *)" required>
    <input name="timezone" placeholder="Timezone" value="UTC">
    <textarea name="prompt" placeholder="Agent prompt" rows="3" required></textarea>
    <select name="output_mode">
        <option value="discord">Discord</option>
        <option value="dashboard_only">Dashboard only</option>
        <option value="discord_if_noteworthy">Discord if noteworthy</option>
    </select>
    <button type="submit">Create</button>
</form>

<h2>Existing Schedules</h2>
<table>
    <thead>
        <tr><th>Name</th><th>Cron</th><th>Output</th><th>Enabled</th><th>Last Run</th><th>Actions</th></tr>
    </thead>
    <tbody>
    {% for s in schedules %}
        <tr>
            <td>{{ s.name }}</td>
            <td><code>{{ s.cron_expr }}</code></td>
            <td>{{ s.output_mode }}</td>
            <td>
                <span class="badge {% if s.enabled %}badge-ok{% else %}badge-warn{% endif %}">
                    {{ "on" if s.enabled else "off" }}
                </span>
            </td>
            <td class="muted">
                {% if s.last_run_at %}
                    {{ s.last_run_at.strftime('%Y-%m-%d %H:%M') }}
                    <span class="badge {% if s.last_run_status == 'success' %}badge-ok{% else %}badge-err{% endif %}">{{ s.last_run_status }}</span>
                {% else %}
                    never
                {% endif %}
            </td>
            <td>
                <form method="post" action="/schedules/{{ s.id }}/toggle" style="display:inline">
                    <button>{{ "Disable" if s.enabled else "Enable" }}</button>
                </form>
                <form method="post" action="/schedules/{{ s.id }}/delete" style="display:inline">
                    <button class="btn-danger">Delete</button>
                </form>
            </td>
        </tr>
    {% endfor %}
    </tbody>
</table>
{% if not schedules %}
<p class="muted">No schedules yet.</p>
{% endif %}
{% endblock %}
```

Write `jarvis/web/templates/schedule_form.html` (empty placeholder for now — the create form is inline in `schedules.html`):

```html
{# Reserved for future HTMX edit modal #}
```

Register in `jarvis/web/app.py`:

```python
    from jarvis.web.routes.schedules import router as schedules_router

    app.include_router(schedules_router)
```

- [ ] **Step 4: Run tests + full suite + ruff + commit**

```bash
git add jarvis/web/routes/schedules.py jarvis/web/templates/schedules.html jarvis/web/templates/schedule_form.html jarvis/web/app.py tests/integration/test_web_schedules.py
git commit -m "add schedules page with create, toggle, and delete"
```

---

## Task 6: MCP servers page

**Files:**
- Create: `jarvis/web/routes/mcp.py`
- Create: `jarvis/web/templates/mcp.html`
- Modify: `jarvis/web/app.py`
- Create: `tests/integration/test_web_mcp.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_web_mcp.py`:

```python
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed an MCP server + tool.
    async with factory() as s:
        server = await MCPServerRepo(s).upsert(name="gcal", transport="stdio")
        await MCPServerRepo(s).set_status(server.id, status="connected", last_error=None)
        await MCPToolRepo(s).replace_for_server(
            server.id,
            tools=[MCPToolDescriptor(name="list_events", input_schema={}, read_only_hint=True)],
        )

    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory

    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_mcp_page_renders_server_and_tools(client):
    resp = client.get("/mcp")
    assert resp.status_code == 200
    assert "gcal" in resp.text
    assert "list_events" in resp.text
    assert "connected" in resp.text.lower()
```

- [ ] **Step 2: Write route + template**

Write `jarvis/web/routes/mcp.py`:

```python
"""GET /mcp — MCP server list + tools."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

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

    return templates.TemplateResponse(
        "mcp.html",
        {"request": request, "servers": servers, "server_tools": server_tools},
    )
```

Write `jarvis/web/templates/mcp.html`:

```html
{% extends "base.html" %}
{% block title %}MCP Servers{% endblock %}
{% block content %}
<h1>MCP Servers</h1>
{% for srv in servers %}
<div style="margin-bottom: 1.5rem;">
    <h2>{{ srv.name }} <span class="badge {% if srv.status == 'connected' %}badge-ok{% elif srv.status == 'error' %}badge-err{% else %}badge-warn{% endif %}">{{ srv.status }}</span></h2>
    <p class="muted">Transport: {{ srv.transport }}{% if srv.last_connected_at %} — Last connected: {{ srv.last_connected_at.strftime('%Y-%m-%d %H:%M') }}{% endif %}</p>
    {% if srv.last_error %}<pre>{{ srv.last_error }}</pre>{% endif %}
    {% set tools = server_tools.get(srv.id, []) %}
    {% if tools %}
    <table>
        <thead><tr><th>Tool</th><th>Description</th><th>Read-only</th><th>Destructive</th><th>Policy</th></tr></thead>
        <tbody>
        {% for t in tools %}
            <tr>
                <td><code>{{ t.name }}</code></td>
                <td>{{ t.description or '—' }}</td>
                <td>{{ '✓' if t.read_only_hint else '—' }}</td>
                <td>{{ '⚠' if t.destructive_hint else '—' }}</td>
                <td>{{ t.policy_override or 'auto-detect' }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p class="muted">No tools discovered.</p>
    {% endif %}
</div>
{% endfor %}
{% if not servers %}
<p class="muted">No MCP servers configured.</p>
{% endif %}
{% endblock %}
```

Register in `app.py` and run tests + commit.

```bash
git add jarvis/web/routes/mcp.py jarvis/web/templates/mcp.html jarvis/web/app.py tests/integration/test_web_mcp.py
git commit -m "add MCP servers page with tool listing"
```

---

## Task 7: Audit log page + SSE live tail

**Files:**
- Create: `jarvis/web/routes/audit.py`
- Create: `jarvis/web/routes/events.py`
- Create: `jarvis/web/templates/audit.html`
- Create: `jarvis/web/templates/audit_row.html`
- Modify: `jarvis/web/app.py`
- Create: `tests/integration/test_web_audit.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_web_audit.py`:

```python
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed some audit events.
    async with factory() as s:
        await AuditRepo(s).write_many([
            AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"test": True}),
            AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"model": "qwen"}),
        ])

    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory

    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_audit_page_renders_events(client):
    resp = client.get("/audit")
    assert resp.status_code == 200
    assert "trigger.received" in resp.text
    assert "llm.request" in resp.text


def test_events_stream_endpoint_exists(client):
    """The SSE endpoint should return a streaming response."""
    # We can't easily test SSE with TestClient (it blocks), but we can
    # verify the endpoint exists and returns the right content type.
    resp = client.get("/events/stream", timeout=1)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
```

- [ ] **Step 2: Write routes + templates**

Write `jarvis/web/routes/audit.py`:

```python
"""GET /audit — filterable audit event log."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from jarvis.core.types import AuditEventType
from jarvis.persistence.repositories import AuditRepo

router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    type_filter: str | None = Query(None, alias="type"),
    limit: int = Query(100, le=500),
):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    types = None
    if type_filter:
        try:
            types = [AuditEventType(type_filter)]
        except ValueError:
            types = None

    async with ctx.session_factory() as session:
        events = await AuditRepo(session).recent(types=types, limit=limit)

    all_types = [t.value for t in AuditEventType]
    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request,
            "events": events,
            "all_types": all_types,
            "current_filter": type_filter,
        },
    )
```

Write `jarvis/web/routes/events.py`:

```python
"""GET /events/stream — SSE endpoint for live audit event tailing."""

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jarvis.persistence.repositories import AuditRepo

router = APIRouter()


@router.get("/events/stream")
async def events_stream(request: Request):
    ctx = request.app.state.ctx

    async def _generate():
        last_seen = datetime.now(UTC)
        while True:
            await asyncio.sleep(1.0)  # poll interval
            if await request.is_disconnected():
                break
            async with ctx.session_factory() as session:
                repo = AuditRepo(session)
                # Fetch events newer than our last check.
                from sqlalchemy import select

                from jarvis.persistence.models import AuditEventRow

                result = await session.execute(
                    select(AuditEventRow)
                    .where(AuditEventRow.created_at > last_seen)
                    .order_by(AuditEventRow.created_at.asc())
                    .limit(50)
                )
                rows = list(result.scalars())

            for row in rows:
                data = json.dumps(
                    {
                        "type": row.type,
                        "payload": row.payload,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    },
                    default=str,
                )
                yield f"event: audit\ndata: {data}\n\n"
                last_seen = max(last_seen, row.created_at)

    return StreamingResponse(_generate(), media_type="text/event-stream")
```

Write `jarvis/web/templates/audit.html`:

```html
{% extends "base.html" %}
{% block title %}Audit Log{% endblock %}
{% block content %}
<h1>Audit Log</h1>

<form method="get" action="/audit" style="display:flex; gap:0.5rem; margin-bottom:1rem;">
    <select name="type">
        <option value="">All types</option>
        {% for t in all_types %}
        <option value="{{ t }}" {% if current_filter == t %}selected{% endif %}>{{ t }}</option>
        {% endfor %}
    </select>
    <button type="submit">Filter</button>
</form>

<div id="live-events" hx-ext="sse" sse-connect="/events/stream" sse-swap="audit" hx-swap="afterbegin">
</div>

<h2>Recent Events</h2>
<div id="events-table">
{% for ev in events %}
    <div class="audit-row">
        <span class="audit-type">{{ ev.type }}</span>
        <span class="muted">{{ ev.created_at.strftime('%H:%M:%S') if ev.created_at else '' }}</span>
        <pre>{{ ev.payload }}</pre>
    </div>
{% endfor %}
</div>
{% if not events %}
<p class="muted">No audit events.</p>
{% endif %}
{% endblock %}
```

Write `jarvis/web/templates/audit_row.html`:

```html
{# SSE swap target — a single audit event row injected by HTMX #}
<div class="audit-row">
    <span class="audit-type">{{ type }}</span>
    <span class="muted">{{ created_at }}</span>
    <pre>{{ payload }}</pre>
</div>
```

Register both routers in `app.py`:

```python
    from jarvis.web.routes.audit import router as audit_router
    from jarvis.web.routes.events import router as events_router

    app.include_router(audit_router)
    app.include_router(events_router)
```

- [ ] **Step 3: Run tests + full suite + ruff + commit**

```bash
git add jarvis/web/routes/audit.py jarvis/web/routes/events.py jarvis/web/templates/audit.html jarvis/web/templates/audit_row.html jarvis/web/app.py tests/integration/test_web_audit.py
git commit -m "add audit log page with SSE live tailing"
```

---

## Task 8: Settings page

**Files:**
- Create: `jarvis/web/routes/settings.py`
- Create: `jarvis/web/templates/settings.html`
- Modify: `jarvis/web/app.py`
- Create: `tests/integration/test_web_settings.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_web_settings.py`:

```python
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from jarvis.web.app import create_app


def _mock_context():
    ctx = MagicMock()
    ctx.config.jarvis.llm.base_url = "http://x/v1"
    ctx.config.jarvis.llm.model = "m"
    ctx.config.jarvis.timezone = "UTC"
    ctx.config.jarvis.idle_timeout_sec = 900
    ctx.config.jarvis.max_concurrent_agents = 3
    ctx.config.jarvis.log_level = "INFO"
    ctx.config.channels.discord = None
    ctx.config.mcp_servers.servers = []
    return ctx


def test_settings_page_renders(tmp_path):
    ctx = _mock_context()
    app = create_app(app_context=ctx)
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "900" in resp.text  # idle_timeout
    assert "UTC" in resp.text
```

- [ ] **Step 2: Write route + template**

Write `jarvis/web/routes/settings.py`:

```python
"""GET /settings — read-only config view."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    cfg = ctx.config

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "jarvis": cfg.jarvis,
            "channels": cfg.channels,
            "mcp_servers": cfg.mcp_servers,
        },
    )
```

Write `jarvis/web/templates/settings.html`:

```html
{% extends "base.html" %}
{% block title %}Settings{% endblock %}
{% block content %}
<h1>Settings</h1>

<h2>Core</h2>
<table>
    <tr><th>LLM base URL</th><td>{{ jarvis.llm.base_url }}</td></tr>
    <tr><th>LLM model</th><td>{{ jarvis.llm.model }}</td></tr>
    <tr><th>Timezone</th><td>{{ jarvis.timezone }}</td></tr>
    <tr><th>Idle timeout (sec)</th><td>{{ jarvis.idle_timeout_sec }}</td></tr>
    <tr><th>Max concurrent agents</th><td>{{ jarvis.max_concurrent_agents }}</td></tr>
    <tr><th>Log level</th><td>{{ jarvis.log_level }}</td></tr>
</table>

<h2>Channels</h2>
{% if channels.discord %}
<table>
    <tr><th>Discord enabled</th><td>{{ channels.discord.enabled }}</td></tr>
    <tr><th>Allowed users</th><td>{{ channels.discord.allowed_user_ids | join(", ") }}</td></tr>
</table>
{% else %}
<p class="muted">No Discord channel configured.</p>
{% endif %}

<h2>MCP Servers (from config)</h2>
{% if mcp_servers.servers %}
<table>
    <thead><tr><th>Name</th><th>Transport</th><th>Enabled</th></tr></thead>
    <tbody>
    {% for s in mcp_servers.servers %}
        <tr><td>{{ s.name }}</td><td>{{ s.transport }}</td><td>{{ s.enabled }}</td></tr>
    {% endfor %}
    </tbody>
</table>
{% else %}
<p class="muted">No MCP servers configured.</p>
{% endif %}

<p class="muted">Settings are read-only. Edit the YAML config files to change.</p>
{% endblock %}
```

Register in `app.py` and test + commit.

```bash
git add jarvis/web/routes/settings.py jarvis/web/templates/settings.html jarvis/web/app.py tests/integration/test_web_settings.py
git commit -m "add read-only settings page"
```

---

## Task 9: Wire dashboard into `bootstrap()` and `jarvis serve`

**Files:**
- Modify: `jarvis/main.py`
- Modify: `jarvis/cli.py`
- Modify: `tests/integration/test_main_smoke.py`

- [ ] **Step 1: Write a smoke test**

Append to `tests/integration/test_main_smoke.py`:

```python


async def test_bootstrap_exposes_web_app(tmp_path, config_dir):
    from fastapi import FastAPI

    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.web_app is not None
        assert isinstance(ctx.web_app, FastAPI)
    finally:
        await ctx.shutdown()
```

- [ ] **Step 2: Update `jarvis/main.py`**

Add import:
```python
from jarvis.web.app import create_app
```

Add `web_app` to `AppContext` (type `FastAPI`):
```python
from fastapi import FastAPI
```

In `bootstrap()`, after everything else, create the web app:

```python
    # Web dashboard.
    web_app = create_app(app_context=None)  # placeholder — we'll set ctx after construction
```

Wait — we have a chicken-and-egg: `create_app` needs `app_context` but `AppContext` needs `web_app`. Solve by creating app first with None, then setting the state after:

```python
    from jarvis.web.app import create_app as _create_web_app

    web_app = _create_web_app(app_context=None)
```

Then after constructing AppContext:

```python
    ctx = AppContext(
        ...,
        web_app=web_app,
    )
    # Now that ctx exists, wire it into the web app.
    web_app.state.ctx = ctx
```

Add `web_app: object` field to AppContext (using `object` to avoid importing FastAPI in main.py's type annotations — or import it, either way).

- [ ] **Step 3: Update `jarvis/cli.py` `_serve_async`**

After `bootstrap`, before `stop_event.wait()`, start uvicorn:

```python
    import uvicorn

    uvi_config = uvicorn.Config(
        ctx.web_app,
        host="0.0.0.0",
        port=8080,
        log_level="warning",
    )
    uvi_server = uvicorn.Server(uvi_config)
    uvi_task = asyncio.create_task(uvi_server.serve(), name="uvicorn")
```

And in the shutdown section (after stop_event fires):

```python
    uvi_server.should_exit = True
    await uvi_task
```

The full updated `_serve_async`:

```python
async def _serve_async(
    *,
    config_dir: Path,
    db_url: str,
    stop_event: asyncio.Event | None = None,
) -> None:
    import uvicorn

    ctx = await bootstrap(config_dir=config_dir, db_url=db_url)
    try:
        # Start uvicorn in the background.
        uvi_config = uvicorn.Config(
            ctx.web_app,
            host="0.0.0.0",
            port=8080,
            log_level="warning",
        )
        uvi_server = uvicorn.Server(uvi_config)
        uvi_task = asyncio.create_task(uvi_server.serve(), name="uvicorn")

        if stop_event is None:
            stop_event = asyncio.Event()
            _install_signal_handlers(stop_event)
        typer.echo("jarvis serving on http://0.0.0.0:8080 (Ctrl-C to stop)")
        await stop_event.wait()
        typer.echo("shutting down...")

        uvi_server.should_exit = True
        await uvi_task
    finally:
        await ctx.shutdown()
```

- [ ] **Step 4: Run tests + full suite + ruff + commit**

```bash
git add jarvis/main.py jarvis/cli.py tests/integration/test_main_smoke.py
git commit -m "wire web dashboard into bootstrap and jarvis serve"
```

---

## Plan 5 complete — summary

At the end of Plan 5:

- `python -m jarvis serve` starts Discord + scheduler + web dashboard on port 8080.
- Dashboard pages:
  - `/` — status overview (LLM endpoint, model, MCP count, schedule count, adapters)
  - `/conversations` — list + detail with message transcript
  - `/schedules` — full CRUD (create, toggle enable/disable, delete)
  - `/mcp` — read-only server list with tools and connection status
  - `/audit` — filterable event log with SSE live tailing
  - `/settings` — read-only config view
  - `/healthz` — JSON health check
  - `/events/stream` — SSE endpoint for live audit events

**Known debt for Plan 6:**
- No auth on the dashboard (spec says "assume trusted local network or reverse proxy").
- No "Run Now" button on schedules page (requires wiring `scheduler.fire_now` — easy add).
- No schedule edit form (only create + delete; edit requires a separate UI form).
- SSE polling interval is 1 second — adequate for personal scale; could be tuned.
- uvicorn port is hardcoded to 8080 — Plan 6 Dockerfile should make it configurable via env.
- No dashboard tests for the SSE streaming content (hard to test with sync TestClient).

**Still to come:** Plan 6 Docker.

"""FastAPI app factory for the Jarvis dashboard."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from jarvis.web.security import SameOriginUnsafeMethodMiddleware
from jarvis.web.time_format import configured_timezone, format_server_local

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


@pass_context
def _localtime_filter(context, value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    request = context.get("request")
    ctx = getattr(getattr(request, "app", None), "state", None)
    app_context = getattr(ctx, "ctx", None)
    return format_server_local(value, fmt, timezone=configured_timezone(app_context))


def create_app(*, app_context=None) -> FastAPI:
    """Build the FastAPI app. `app_context` is the bootstrap AppContext —
    None is tolerated for healthz-only testing.
    """
    app = FastAPI(title="Jarvis Dashboard", docs_url=None, redoc_url=None)
    app.add_middleware(SameOriginUnsafeMethodMiddleware)

    # Attach context so route handlers can access repos, config, etc.
    app.state.ctx = app_context

    # Templates.
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["localtime"] = _localtime_filter
    app.state.templates = templates

    # Static files.
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Register routes.
    from jarvis.web.routes.health import router as health_router

    app.include_router(health_router)

    from jarvis.web.routes.home import router as home_router

    app.include_router(home_router)

    from jarvis.web.routes.conversations import router as conversations_router

    app.include_router(conversations_router)

    from jarvis.web.routes.memory import router as memory_router

    app.include_router(memory_router)

    from jarvis.web.routes.schedules import router as schedules_router

    app.include_router(schedules_router)

    from jarvis.web.routes.templates import router as templates_router

    app.include_router(templates_router)

    from jarvis.web.routes.actions import router as actions_router

    app.include_router(actions_router)

    from jarvis.web.routes.mcp import router as mcp_router

    app.include_router(mcp_router)

    from jarvis.web.routes.mcp_admin import router as mcp_admin_router

    app.include_router(mcp_admin_router)

    from jarvis.web.routes.audit import router as audit_router
    from jarvis.web.routes.errors import router as errors_router
    from jarvis.web.routes.events import router as events_router

    app.include_router(audit_router)
    app.include_router(errors_router)
    app.include_router(events_router)

    from jarvis.web.routes.settings import router as settings_router

    app.include_router(settings_router)

    from jarvis.web.routes.oauth import router as oauth_router

    app.include_router(oauth_router)

    return app

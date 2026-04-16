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
        request,
        "settings.html",
        {
            "jarvis": cfg.jarvis,
            "channels": cfg.channels,
            "mcp_servers": cfg.mcp_servers,
        },
    )

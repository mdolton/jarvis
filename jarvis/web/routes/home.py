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
        request,
        "home.html",
        {
            "llm_url": llm_url,
            "llm_model": llm_model,
            "mcp_count": mcp_count,
            "schedule_count": schedule_count,
            "adapters": adapters,
        },
    )

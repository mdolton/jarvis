"""GET / — status overview and dashboard manual runs."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from jarvis.web.diagnostics import collect_diagnostics

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    return templates.TemplateResponse(request, "home.html", await _home_context(ctx))


@router.post("/manual-runs", response_class=HTMLResponse)
async def manual_run(request: Request, prompt: str = Form(...)):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    data = await _home_context(ctx)
    try:
        result = await ctx.dispatcher.dispatch_manual(user="dashboard", prompt=prompt)
        data["manual_result"] = result.final_output
    except Exception as exc:
        data["manual_error"] = str(exc)
    data["manual_prompt"] = prompt
    return templates.TemplateResponse(request, "home.html", data)


async def _home_context(ctx):
    llm_url = ctx.config.jarvis.llm.base_url if ctx else "n/a"
    llm_model = ctx.config.jarvis.llm.model if ctx else "n/a"
    mcp_count = len(ctx.mcp_manager.agent_mcp_servers()) if ctx else 0
    schedule_count = ctx.scheduler.active_job_count() if ctx else 0
    adapters = [a.kind for a in ctx.channel_adapters] if ctx else []

    diagnostics = await collect_diagnostics(ctx) if ctx else {"status": "unknown", "components": {}}

    return {
        "llm_url": llm_url,
        "llm_model": llm_model,
        "mcp_count": mcp_count,
        "schedule_count": schedule_count,
        "adapters": adapters,
        "diagnostics": diagnostics,
    }

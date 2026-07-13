"""GET /settings — config view with live model selection; POST /settings/model."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.web.step_up import require_step_up

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    cfg = ctx.config

    catalog = await ctx.model_catalog.list_models()
    selection = ctx.model_store.selection()
    selection_unavailable = selection is not None and catalog.ok and selection not in catalog.models

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "jarvis": cfg.jarvis,
            "channels": cfg.channels,
            "mcp_servers": cfg.mcp_servers,
            "available_models": catalog.models,
            "catalog_ok": catalog.ok,
            "model_selection": selection,
            "config_model": cfg.jarvis.llm.model,
            "selection_unavailable": selection_unavailable,
        },
    )


@router.post("/settings/model", dependencies=[Depends(require_step_up)])
async def set_model(request: Request, model: str = Form("")):
    ctx = request.app.state.ctx
    old = ctx.model_store.current()
    sel = model.strip() or None
    await ctx.model_store.set(sel)
    await ctx.audit.emit(
        AuditEvent(
            type=AuditEventType.MODEL_CHANGED,
            payload={"old": old, "new": ctx.model_store.current(), "source": "dashboard"},
        )
    )
    return RedirectResponse(url="/settings", status_code=303)

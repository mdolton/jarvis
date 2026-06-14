"""GET /errors — focused dashboard view for error audit events."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from jarvis.persistence.repositories import AuditRepo
from jarvis.web.error_log import ERROR_AUDIT_TYPES

router = APIRouter()


@router.get("/errors", response_class=HTMLResponse)
async def error_log_page(
    request: Request,
    limit: int = Query(100, le=500),
):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        events = await AuditRepo(session).recent(types=ERROR_AUDIT_TYPES, limit=limit)

    return templates.TemplateResponse(
        request,
        "errors.html",
        {
            "events": events,
        },
    )

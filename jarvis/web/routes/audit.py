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
        request,
        "audit.html",
        {
            "events": events,
            "all_types": all_types,
            "current_filter": type_filter,
        },
    )

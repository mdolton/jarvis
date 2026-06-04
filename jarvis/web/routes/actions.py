"""Action Inbox dashboard routes."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import ActionRepo

router = APIRouter()


@router.get("/actions", response_class=HTMLResponse)
async def actions_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        actions = await ActionRepo(session).list_recent(limit=100)
    return templates.TemplateResponse(request, "actions.html", {"actions": actions})


@router.get("/actions/{action_id}", response_class=HTMLResponse)
async def action_detail(request: Request, action_id: UUID):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        action = await ActionRepo(session).get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")

    return templates.TemplateResponse(
        request,
        "action_detail.html",
        {
            "action": action,
            "arguments_pretty": json.dumps(action.arguments_json, indent=2, sort_keys=True),
        },
    )


@router.post("/actions/{action_id}/approve")
async def approve_action(request: Request, action_id: UUID):
    await request.app.state.ctx.action_service.approve(action_id)
    return RedirectResponse(url=f"/actions/{action_id}", status_code=303)


@router.post("/actions/{action_id}/reject")
async def reject_action(request: Request, action_id: UUID, reason: str = Form("")):
    await request.app.state.ctx.action_service.reject(action_id, reason=reason.strip() or None)
    return RedirectResponse(url=f"/actions/{action_id}", status_code=303)

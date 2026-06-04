"""Action Inbox dashboard routes."""

from __future__ import annotations

import asyncio
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
        actions = await ActionRepo(session).list_for_inbox(limit=100)
    items = [
        {"action": action, "arguments_preview": _arguments_preview(action.arguments_json)}
        for action in actions
    ]
    return templates.TemplateResponse(request, "actions.html", {"actions": items})


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
    try:
        await asyncio.shield(request.app.state.ctx.action_service.approve(action_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="action is not pending") from exc
    return RedirectResponse(url=f"/actions/{action_id}", status_code=303)


@router.post("/actions/{action_id}/reject")
async def reject_action(request: Request, action_id: UUID, reason: str = Form("")):
    try:
        await asyncio.shield(
            request.app.state.ctx.action_service.reject(action_id, reason=reason.strip() or None)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="action is not pending") from exc
    return RedirectResponse(url=f"/actions/{action_id}", status_code=303)


def _arguments_preview(arguments: dict, *, max_length: int = 120) -> str:
    if not arguments:
        return "n/a"
    preview = json.dumps(arguments, sort_keys=True, separators=(", ", ": "))
    if len(preview) <= max_length:
        return preview
    return f"{preview[: max_length - 3]}..."

"""Memory dashboard routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo

router = APIRouter()


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        preference_repo = MemoryPreferenceRepo(session)
        entry_repo = MemoryEntryRepo(session)
        preferences = await preference_repo.list_for_dashboard(limit=100)
        entries = await entry_repo.list_recent(limit=100)
        evidence_by_entry = await entry_repo.list_evidence_for_entries([entry.id for entry in entries])
        entry_items = [
            {"entry": entry, "evidence": evidence_by_entry.get(entry.id, [])} for entry in entries
        ]

    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "preferences": preferences,
            "entry_items": entry_items,
        },
    )


@router.post("/memory/preferences/{preference_id}/approve")
async def approve_preference(request: Request, preference_id: UUID):
    try:
        async with request.app.state.ctx.session_factory() as session:
            await MemoryPreferenceRepo(session).approve(preference_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="memory preference not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="invalid preference transition") from exc
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/memory/preferences/{preference_id}/reject")
async def reject_preference(request: Request, preference_id: UUID):
    try:
        async with request.app.state.ctx.session_factory() as session:
            await MemoryPreferenceRepo(session).reject(preference_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="memory preference not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="invalid preference transition") from exc
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/memory/preferences/{preference_id}/archive")
async def archive_preference(request: Request, preference_id: UUID):
    try:
        async with request.app.state.ctx.session_factory() as session:
            await MemoryPreferenceRepo(session).archive(preference_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="memory preference not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="invalid preference transition") from exc
    return RedirectResponse(url="/memory", status_code=303)


@router.post("/memory/entries/{entry_id}/archive")
async def archive_entry(request: Request, entry_id: UUID):
    try:
        async with request.app.state.ctx.session_factory() as session:
            await MemoryEntryRepo(session).archive(entry_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="memory entry not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="invalid memory entry transition") from exc
    return RedirectResponse(url="/memory", status_code=303)

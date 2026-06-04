"""Schedule CRUD pages."""

from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import ScheduleRepo

router = APIRouter()


@router.get("/schedules", response_class=HTMLResponse)
async def schedule_list(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    catalog = await ctx.model_catalog.list_models()
    async with ctx.session_factory() as session:
        schedules = await ScheduleRepo(session).list_all()
    available = set(catalog.models) if catalog.ok else None
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "schedules": schedules,
            "available_models": catalog.models,
            "catalog_ok": catalog.ok,
            "available_set": available,
        },
    )


@router.post("/schedules")
async def schedule_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    cron_expr: str = Form(...),
    timezone: str = Form("UTC"),
    prompt: str = Form(...),
    output_mode: str = Form("discord"),
    model: str = Form(""),
    discord_user_id: str = Form(""),
):
    ctx = request.app.state.ctx
    target_user = discord_user_id.strip() or _default_discord_user_id(ctx)
    async with ctx.session_factory() as session:
        await ScheduleRepo(session).create(
            name=name,
            description=description,
            cron_expr=cron_expr,
            timezone=timezone,
            prompt=prompt,
            output_mode=output_mode,
            notify_on_error=True,
            enabled=True,
            model=model.strip() or None,
            discord_user_id=target_user,
        )
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle")
async def schedule_toggle(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = ScheduleRepo(session)
        row = await repo.get(schedule_id)
        if row:
            await repo.set_enabled(schedule_id, not row.enabled)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/run")
async def schedule_run_now(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    await ctx.scheduler.fire_now(schedule_id)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
async def schedule_delete(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        await ScheduleRepo(session).delete(schedule_id)
    return RedirectResponse(url="/schedules", status_code=303)


def _default_discord_user_id(ctx) -> str | None:
    discord = getattr(ctx.config.channels, "discord", None)
    if discord is None or not discord.enabled:
        return None
    if len(discord.allowed_user_ids) == 1:
        return discord.allowed_user_ids[0]
    return None

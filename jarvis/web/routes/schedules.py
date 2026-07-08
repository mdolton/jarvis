"""Schedule CRUD pages."""

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.core.types import AuditEventType
from jarvis.persistence.repositories import AuditRepo, DigestTemplateRepo, ScheduleRepo
from jarvis.scheduler.scheduler import validate_schedule_timing

router = APIRouter()


@router.get("/schedules", response_class=HTMLResponse)
async def schedule_list(request: Request, template_id: str | None = Query(default=None)):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    catalog = await ctx.model_catalog.list_models()
    template_warning = None
    selected_template = None
    parsed_template_id = None
    if template_id is not None:
        try:
            parsed_template_id = UUID(template_id)
        except ValueError:
            template_warning = "Template not found or disabled."
    async with ctx.session_factory() as session:
        schedule_repo = ScheduleRepo(session)
        template_repo = DigestTemplateRepo(session)
        schedules = await schedule_repo.list_all()
        digest_templates = await template_repo.list_enabled()
        schedule_error_events = await AuditRepo(session).recent(
            types=[AuditEventType.SCHEDULE_ERROR],
            limit=500,
        )
        if parsed_template_id is not None:
            selected_template = await template_repo.get(parsed_template_id)
            if selected_template is None or not selected_template.enabled:
                selected_template = None
                template_warning = "Template not found or disabled."
    schedule_error_links = _schedule_error_links(schedule_error_events)
    available = set(catalog.models) if catalog.ok else None
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "schedules": schedules,
            "available_models": catalog.models,
            "catalog_ok": catalog.ok,
            "available_set": available,
            "digest_templates": digest_templates,
            "selected_template": selected_template,
            "template_warning": template_warning,
            "default_timezone": ctx.config.jarvis.timezone,
            "schedule_error_links": schedule_error_links,
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
    try:
        validate_schedule_timing(cron_expr, timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid schedule: {exc}") from exc
    target_user = discord_user_id.strip() or _default_discord_user_id(ctx)
    async with ctx.session_factory() as session:
        row = await ScheduleRepo(session).create(
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
    await ctx.scheduler.on_created(row)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle")
async def schedule_toggle(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = ScheduleRepo(session)
        row = await repo.get(schedule_id)
        if row:
            await repo.set_enabled(schedule_id, not row.enabled)
    if row:
        async with ctx.session_factory() as session:
            row = await ScheduleRepo(session).get(schedule_id)
        await ctx.scheduler.on_toggled(row)
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
    await ctx.scheduler.on_deleted(schedule_id)
    return RedirectResponse(url="/schedules", status_code=303)


def _default_discord_user_id(ctx) -> str | None:
    discord = getattr(ctx.config.channels, "discord", None)
    if discord is None or not discord.enabled:
        return None
    if len(discord.allowed_user_ids) == 1:
        return discord.allowed_user_ids[0]
    return None


def _schedule_error_links(events) -> dict[str, str]:
    links: dict[str, str] = {}
    for event in events:
        schedule_id = event.payload.get("schedule_id")
        if not schedule_id or schedule_id in links:
            continue
        links[schedule_id] = f"/errors#event-{event.id}"
    return links

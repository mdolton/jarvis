"""Schedule CRUD pages."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.core.types import AuditEventType
from jarvis.persistence.repositories import AuditRepo, DigestTemplateRepo, ScheduleRepo
from jarvis.scheduler.scheduler import validate_schedule_timing
from jarvis.web.step_up import require_step_up

router = APIRouter()

_VALID_OUTPUT_MODES = {"discord", "dashboard_only", "discord_if_noteworthy"}


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
            "available_mcp_servers": ctx.mcp_manager.server_names(),
        },
    )


@router.post("/schedules", dependencies=[Depends(require_step_up)])
async def schedule_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    cron_expr: str = Form(...),
    timezone: str = Form("UTC"),
    prompt: str = Form(...),
    output_mode: str = Form("discord"),
    model: str = Form(""),
    mcp_servers: list[str] = Form(default=[]),
    discord_user_id: str = Form(""),
):
    ctx = request.app.state.ctx
    _validate_timing(cron_expr, timezone)
    _validate_output_mode(output_mode)
    scope = _parse_scope(mcp_servers)
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
            mcp_servers=scope,
            discord_user_id=target_user,
        )
    await ctx.scheduler.on_created(row)
    return RedirectResponse(url="/schedules", status_code=303)


@router.get("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
async def schedule_edit_form(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    catalog = await ctx.model_catalog.list_models()
    async with ctx.session_factory() as session:
        row = await ScheduleRepo(session).get(schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return templates.TemplateResponse(
        request,
        "schedule_form.html",
        {
            "schedule": row,
            "available_models": catalog.models,
            "catalog_ok": catalog.ok,
            "available_set": set(catalog.models) if catalog.ok else None,
            "available_mcp_servers": ctx.mcp_manager.server_names(),
        },
    )


@router.post("/schedules/{schedule_id}", dependencies=[Depends(require_step_up)])
async def schedule_update(
    request: Request,
    schedule_id: UUID,
    name: str = Form(...),
    description: str = Form(""),
    cron_expr: str = Form(...),
    timezone: str = Form("UTC"),
    prompt: str = Form(...),
    output_mode: str = Form("discord"),
    model: str = Form(""),
    mcp_servers: list[str] = Form(default=[]),
    discord_user_id: str = Form(""),
    notify_on_error: bool = Form(default=False),
):
    ctx = request.app.state.ctx
    _validate_timing(cron_expr, timezone)
    _validate_output_mode(output_mode)
    async with ctx.session_factory() as session:
        repo = ScheduleRepo(session)
        if await repo.get(schedule_id) is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        await repo.update(
            schedule_id,
            name=name.strip(),
            description=description.strip(),
            cron_expr=cron_expr.strip(),
            timezone=timezone.strip(),
            prompt=prompt,
            output_mode=output_mode.strip(),
            model=model.strip() or None,
            mcp_servers=_parse_scope(mcp_servers) or None,
            # Unlike create, a blank recipient here is an explicit clear: the
            # form was prefilled with the current value, so emptying it is a
            # deliberate act, not "I didn't say".
            discord_user_id=discord_user_id.strip() or None,
            notify_on_error=notify_on_error,
        )
    # Fresh session: the factory is expire_on_commit=False, so re-reading
    # through `repo` would hand back the pre-update identity-map instance.
    async with ctx.session_factory() as session:
        row = await ScheduleRepo(session).get(schedule_id)
    if row is not None:
        await ctx.scheduler.on_updated(row)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/scope", dependencies=[Depends(require_step_up)])
async def schedule_set_scope(
    request: Request,
    schedule_id: UUID,
    mcp_servers: list[str] = Form(default=[]),
):
    """Re-scope an existing schedule's MCP servers.

    No scheduler notification: unlike cron or timezone, the scope is read from
    the row at fire time, so the next run picks it up on its own.
    """
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = ScheduleRepo(session)
        if await repo.get(schedule_id) is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        await repo.update(schedule_id, mcp_servers=_parse_scope(mcp_servers) or None)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle", dependencies=[Depends(require_step_up)])
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
        if row is not None:
            await ctx.scheduler.on_toggled(row)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/run", dependencies=[Depends(require_step_up)])
async def schedule_run_now(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    await ctx.scheduler.fire_now(schedule_id)
    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete", dependencies=[Depends(require_step_up)])
async def schedule_delete(request: Request, schedule_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        await ScheduleRepo(session).delete(schedule_id)
    await ctx.scheduler.on_deleted(schedule_id)
    return RedirectResponse(url="/schedules", status_code=303)


def _validate_timing(cron_expr: str, timezone: str) -> None:
    try:
        validate_schedule_timing(cron_expr, timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid schedule: {exc}") from exc


def _validate_output_mode(output_mode: str) -> None:
    if output_mode.strip() not in _VALID_OUTPUT_MODES:
        raise HTTPException(status_code=400, detail="Invalid output mode")


def _parse_scope(mcp_servers: list[str]) -> list[str]:
    return [name for name in (s.strip() for s in mcp_servers) if name]


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

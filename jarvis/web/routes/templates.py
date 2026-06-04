"""Digest template dashboard routes."""

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import DigestTemplateRepo

router = APIRouter()

_VALID_OUTPUT_MODES = {
    "discord",
    "dashboard_only",
    "discord_if_noteworthy",
}


@router.get("/templates", response_class=HTMLResponse)
async def template_list(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        rows = await DigestTemplateRepo(session).list_enabled()
    return templates.TemplateResponse(request, "templates.html", {"templates": rows})


@router.get("/templates/new", response_class=HTMLResponse)
async def template_new(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        rows = await DigestTemplateRepo(session).list_enabled()
    return templates.TemplateResponse(request, "templates.html", {"templates": rows})


@router.post("/templates")
async def template_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form(...),
    prompt: str = Form(...),
    default_cron_expr: str = Form(...),
    default_timezone: str = Form("UTC"),
    default_output_mode: str = Form("discord"),
    default_model: str = Form(""),
    default_discord_user_id: str = Form(""),
):
    fields = _validated_fields(
        name=name,
        description=description,
        category=category,
        prompt=prompt,
        default_cron_expr=default_cron_expr,
        default_timezone=default_timezone,
        default_output_mode=default_output_mode,
        default_model=default_model,
        default_discord_user_id=default_discord_user_id,
    )
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        await DigestTemplateRepo(session).create(
            key=None,
            built_in=False,
            enabled=True,
            **fields,
        )
    return RedirectResponse(url="/templates", status_code=303)


@router.get("/templates/{template_id}", response_class=HTMLResponse)
async def template_detail(request: Request, template_id: UUID):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        row = await DigestTemplateRepo(session).get(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Digest template not found")
    return templates.TemplateResponse(
        request,
        "template_detail.html",
        {"template": row},
    )


@router.post("/templates/{template_id}")
async def template_update(
    request: Request,
    template_id: UUID,
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form(...),
    prompt: str = Form(...),
    default_cron_expr: str = Form(...),
    default_timezone: str = Form("UTC"),
    default_output_mode: str = Form("discord"),
    default_model: str = Form(""),
    default_discord_user_id: str = Form(""),
):
    fields = _validated_fields(
        name=name,
        description=description,
        category=category,
        prompt=prompt,
        default_cron_expr=default_cron_expr,
        default_timezone=default_timezone,
        default_output_mode=default_output_mode,
        default_model=default_model,
        default_discord_user_id=default_discord_user_id,
    )
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = DigestTemplateRepo(session)
        row = await repo.get(template_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Digest template not found")
        await repo.update(template_id, **fields)
    return RedirectResponse(url=f"/templates/{template_id}", status_code=303)


@router.post("/templates/{template_id}/clone")
async def template_clone(request: Request, template_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = DigestTemplateRepo(session)
        row = await repo.get(template_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Digest template not found")
        await repo.clone(template_id)
    return RedirectResponse(url="/templates", status_code=303)


@router.post("/templates/{template_id}/disable")
async def template_disable(request: Request, template_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = DigestTemplateRepo(session)
        row = await repo.get(template_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Digest template not found")
        if row.built_in:
            raise HTTPException(
                status_code=400,
                detail="built-in digest templates cannot be disabled",
            )
        await repo.disable(template_id)
    return RedirectResponse(url="/templates", status_code=303)


def _validated_fields(
    *,
    name: str,
    description: str,
    category: str,
    prompt: str,
    default_cron_expr: str,
    default_timezone: str,
    default_output_mode: str,
    default_model: str,
    default_discord_user_id: str,
) -> dict[str, str | None]:
    mode = default_output_mode.strip()
    if mode not in _VALID_OUTPUT_MODES:
        raise HTTPException(status_code=400, detail="Invalid output mode")

    fields = {
        "name": name.strip(),
        "description": description.strip(),
        "category": category.strip(),
        "prompt": prompt.strip(),
        "default_cron_expr": default_cron_expr.strip(),
        "default_timezone": default_timezone.strip(),
        "default_output_mode": mode,
        "default_model": default_model.strip() or None,
        "default_discord_user_id": default_discord_user_id.strip() or None,
    }
    for key in ("name", "category", "prompt", "default_cron_expr", "default_timezone"):
        if not fields[key]:
            raise HTTPException(status_code=400, detail=f"{key} is required")
    return fields

"""Step-up re-authentication gate for sensitive dashboard routes.

A 30-day session is fine for READING the dashboard; it is the wrong posture
for the OAuth token vault, the tool allow-list, and other routes that widen
what the agent may do. Those declare `Depends(require_step_up)` and demand a
passkey assertion fresher than auth.step_up_window_minutes, tracked in
sessions.last_auth_at (stamped at login and by POST /auth/step-up/complete).

This is a DEPENDENCY, not middleware, on purpose: the gate is declared on
each route decorator, so the protected set is visible at the call site.

Two challenge shapes, because the gated routes are hit two ways:
- htmx partial swaps: a redirect would be swapped into a table cell, so the
  challenge is 401 + an HX-Trigger event that opens the step-up modal; the
  client replays the original request after the assertion succeeds.
- full-page form posts: a 401 page carrying the submitted fields in a hidden
  form, auto-resubmitted after the assertion — nothing the user typed is lost.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from jarvis.auth.sessions import SessionManager, hash_token
from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.repositories import AuthRepo
from jarvis.web.auth_middleware import LOGIN_PATH, auth_config

# The HX-Trigger event name the step-up modal listens for.
STEP_UP_TRIGGER = "jarvis-step-up-required"


class StepUpRequired(Exception):
    """Live session, stale last_auth_at — challenge for a fresh assertion."""

    def __init__(self, form_fields: list[tuple[str, str]] | None) -> None:
        # Echoed into the full-page challenge's hidden replay form; None means
        # the body could not be captured (non-form content type).
        self.form_fields = form_fields


class StepUpUnauthenticated(Exception):
    """No live session at the step-up gate. Only reachable on routes the
    session middleware exempts (/auth/*); everywhere else the middleware
    already answered. Mirrors the middleware's login response — an anonymous
    caller must get the same reply either side of the gate."""


async def emit_step_up_event(
    ctx, type_: AuditEventType, *, user_email: str, path: str, method: str, **extra
) -> None:
    """Audit-trail a step-up challenge/success/failure (tolerates mocked ctx)."""
    emit = getattr(getattr(ctx, "audit", None), "emit", None)
    if emit is not None:
        await emit(
            AuditEvent(
                type=type_,
                payload={"user": user_email, "path": path, "method": method, **extra},
            )
        )


async def _replayable_form(request: Request) -> list[tuple[str, str]] | None:
    """The request's form fields, if they can round-trip through the challenge
    page's hidden replay form. None for bodies we can't echo (the page then
    tells the user to retry after confirming). Reading here is safe: starlette
    caches the parsed form, and the gated handler never runs on this request.
    """
    if request.method.upper() == "GET":
        return []
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return None
    form = await request.form()
    return [(k, v) for k, v in form.multi_items() if isinstance(v, str)]


async def require_step_up(request: Request) -> None:
    """FastAPI dependency: pass only with a passkey assertion fresher than
    auth.step_up_window_minutes; otherwise raise the challenge."""
    cfg = auth_config(request)
    if cfg is None or not cfg.enabled:
        return
    ctx = request.app.state.ctx
    manager = SessionManager(session_factory=ctx.session_factory, config=cfg)
    raw_token = request.cookies.get(manager.cookie_name)

    # The middleware sets request.state.user on gated pages, but /auth/*
    # (logout-all) is middleware-exempt, so fall back to the cookie itself.
    user = getattr(request.state, "user", None)
    if user is None and raw_token:
        user = await manager.validate(raw_token)
    if user is None or not raw_token:
        raise StepUpUnauthenticated

    async with ctx.session_factory() as session:
        row = await AuthRepo(session).get_session_by_token_hash(hash_token(raw_token))
    if row is None:
        raise StepUpUnauthenticated

    window = timedelta(minutes=cfg.step_up_window_minutes)
    if datetime.now(UTC) - row.last_auth_at < window:
        return

    await emit_step_up_event(
        ctx,
        AuditEventType.AUTH_STEP_UP_CHALLENGED,
        user_email=user.email,
        path=request.url.path,
        method=request.method,
    )
    raise StepUpRequired(form_fields=await _replayable_form(request))


def install_step_up_handlers(app: FastAPI) -> None:
    @app.exception_handler(StepUpRequired)
    async def _challenge(request: Request, exc: StepUpRequired):
        if request.headers.get("hx-request"):
            return PlainTextResponse(
                "step-up authentication required",
                status_code=401,
                headers={"HX-Trigger": json.dumps({STEP_UP_TRIGGER: {"path": request.url.path}})},
            )
        action = request.url.path
        if request.url.query:
            action = f"{action}?{request.url.query}"
        return request.app.state.templates.TemplateResponse(
            request,
            "step_up.html",
            {"action": action, "method": request.method.upper(), "fields": exc.form_fields},
            status_code=401,
        )

    @app.exception_handler(StepUpUnauthenticated)
    async def _unauthenticated(request: Request, exc: StepUpUnauthenticated):
        if request.headers.get("hx-request"):
            return PlainTextResponse(
                "authentication required",
                status_code=401,
                headers={"HX-Redirect": LOGIN_PATH},
            )
        return RedirectResponse(LOGIN_PATH, status_code=302)

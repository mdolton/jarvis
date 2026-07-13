"""WebAuthn passkey routes: registration (session-bound), login, management.

Registration REQUIRES an already-authenticated session — that binding is what
ties a passkey to the account established via the emailed code. /auth/* is
exempt from the session middleware, so these routes validate the session
cookie themselves (same pattern as /auth/logout-all).

Every ceremony failure returns the same generic message; the specific reason
(expired challenge, origin mismatch, bad signature, ...) is logged, never
surfaced — a probing client learns nothing about why it was rejected.
"""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from jarvis.auth.passkeys import PasskeyError, PasskeyService
from jarvis.auth.sessions import SessionManager, hash_token
from jarvis.core.types import AuditEventType
from jarvis.persistence.models import UserRow
from jarvis.persistence.repositories import AuthRepo
from jarvis.web.auth_audit import audit_auth, request_meta
from jarvis.web.auth_middleware import LOGIN_PATH, REGISTER_PATH, auth_config
from jarvis.web.routes.auth import _auth_flow
from jarvis.web.step_up import emit_step_up_event, require_step_up

logger = logging.getLogger(__name__)

router = APIRouter()

PASSKEY_FAILED_MESSAGE = (
    "That passkey couldn't be verified. Try again, or sign in with an emailed code."
)


def _passkey_service(request: Request) -> PasskeyService | None:
    cfg = auth_config(request)
    if cfg is None:
        return None
    return PasskeyService(session_factory=request.app.state.ctx.session_factory, config=cfg)


def _session_manager(request: Request) -> SessionManager | None:
    cfg = auth_config(request)
    if cfg is None:
        return None
    return SessionManager(session_factory=request.app.state.ctx.session_factory, config=cfg)


async def _session_user(request: Request) -> UserRow | None:
    """Validate the session cookie directly — /auth/* bypasses the middleware."""
    manager = _session_manager(request)
    if manager is None:
        return None
    raw_token = request.cookies.get(manager.cookie_name)
    return await manager.validate(raw_token) if raw_token else None


def _failed(status: int) -> JSONResponse:
    return JSONResponse({"verified": False, "error": PASSKEY_FAILED_MESSAGE}, status_code=status)


async def _ceremony_input(request: Request) -> tuple[UUID, str, dict] | None:
    """Parse {challenge_id, credential, ...} out of a ceremony-complete body."""
    try:
        body = await request.json()
        challenge_id = UUID(str(body["challenge_id"]))
        credential = json.dumps(body["credential"])
    except (ValueError, TypeError, KeyError):
        return None
    return challenge_id, credential, body


# -- registration (requires an authenticated session) -----------------


@router.get(REGISTER_PATH, response_class=HTMLResponse)
async def register_page(request: Request):
    user = await _session_user(request)
    if user is None:
        return RedirectResponse(LOGIN_PATH, status_code=302)
    async with request.app.state.ctx.session_factory() as session:
        enrolled = await AuthRepo(session).has_credentials(user.id)
    return request.app.state.templates.TemplateResponse(
        request,
        "auth_passkey_register.html",
        {"email": user.email, "enrolled": enrolled},
    )


@router.post("/auth/passkey/register/begin")
async def register_begin(request: Request):
    user = await _session_user(request)
    service = _passkey_service(request)
    if user is None or service is None:
        return _failed(401)
    options_json, challenge_id = await service.begin_registration(user)
    return JSONResponse({"challenge_id": str(challenge_id), "options": json.loads(options_json)})


@router.post("/auth/passkey/register/complete")
async def register_complete(request: Request):
    user = await _session_user(request)
    service = _passkey_service(request)
    if user is None or service is None:
        return _failed(401)
    parsed = await _ceremony_input(request)
    if parsed is None:
        return _failed(400)
    challenge_id, credential, body = parsed
    name = str(body.get("name") or "")[:128] or None
    ctx = request.app.state.ctx
    try:
        result = await service.complete_registration(
            user, challenge_id=challenge_id, credential=credential, name=name
        )
    except PasskeyError as exc:
        logger.info("passkey registration rejected for %s: %s", user.email, exc)
        await audit_auth(
            ctx,
            AuditEventType.AUTH_PASSKEY_REGISTRATION_FAILED,
            email=user.email,
            **request_meta(request),
        )
        return _failed(400)
    await audit_auth(
        ctx,
        AuditEventType.AUTH_PASSKEY_REGISTERED,
        email=user.email,
        credential_id=result.credential.credential_id,
        name=name,
        **request_meta(request),
    )
    return JSONResponse(
        {
            "verified": True,
            "credential_id": result.credential.credential_id,
            # Present only at first enrollment; shown once, stored hashed.
            "recovery_codes": result.recovery_codes,
        }
    )


# -- login (unauthenticated; success issues a session) -----------------


@router.post("/auth/passkey/login/begin")
async def login_begin(request: Request):
    service = _passkey_service(request)
    if service is None:
        return _failed(401)
    options_json, challenge_id = await service.begin_login()
    return JSONResponse({"challenge_id": str(challenge_id), "options": json.loads(options_json)})


@router.post("/auth/passkey/login/complete")
async def login_complete(request: Request):
    service = _passkey_service(request)
    manager = _session_manager(request)
    if service is None or manager is None:
        return _failed(401)
    ctx = request.app.state.ctx
    flow = _auth_flow(request)
    ip = (request.client.host if request.client else None) or "unknown"
    meta = request_meta(request)
    # Same per-IP failure backoff as the code path — one lockout covers both
    # ways to fail a login.
    if not flow.login_backoff.allowed(ip):
        await audit_auth(ctx, AuditEventType.AUTH_RATE_LIMITED, scope="login_backoff", **meta)
        return _failed(401)
    parsed = await _ceremony_input(request)
    if parsed is None:
        return _failed(400)
    challenge_id, credential, _ = parsed
    try:
        user = await service.complete_login(challenge_id=challenge_id, credential=credential)
    except PasskeyError as exc:
        logger.info("passkey login rejected: %s", exc)
        flow.login_backoff.record_failure(ip)
        await audit_auth(ctx, AuditEventType.AUTH_LOGIN_FAILED, method="passkey", **meta)
        return _failed(401)
    flow.login_backoff.reset(ip)
    await audit_auth(
        ctx, AuditEventType.AUTH_LOGIN_SUCCEEDED, method="passkey", email=user.email, **meta
    )
    raw = await manager.issue_session(user.id, request)
    response = JSONResponse({"verified": True, "redirect": "/"})
    manager.set_session_cookie(response, raw)
    return response


# -- step-up (fresh assertion for sensitive routes) --------------------
#
# Same login ceremony, but bound to the CURRENT session: the asserting user
# must be the session's user, and success stamps sessions.last_auth_at so
# require_step_up passes for the next step_up_window_minutes.


@router.post("/auth/step-up/begin")
async def step_up_begin(request: Request):
    user = await _session_user(request)
    service = _passkey_service(request)
    if user is None or service is None:
        return _failed(401)
    options_json, challenge_id = await service.begin_login()
    return JSONResponse({"challenge_id": str(challenge_id), "options": json.loads(options_json)})


@router.post("/auth/step-up/complete")
async def step_up_complete(request: Request):
    user = await _session_user(request)
    service = _passkey_service(request)
    manager = _session_manager(request)
    if user is None or service is None or manager is None:
        return _failed(401)
    parsed = await _ceremony_input(request)
    if parsed is None:
        return _failed(400)
    challenge_id, credential, _ = parsed
    ctx = request.app.state.ctx

    async def _audit_failure(reason: str) -> None:
        await emit_step_up_event(
            ctx,
            AuditEventType.AUTH_STEP_UP_FAILED,
            user_email=user.email,
            request=request,
            reason=reason,
        )

    try:
        asserted = await service.complete_login(challenge_id=challenge_id, credential=credential)
    except PasskeyError as exc:
        logger.info("step-up assertion rejected for %s: %s", user.email, exc)
        await _audit_failure("assertion failed")
        return _failed(401)
    if asserted.id != user.id:
        logger.info(
            "step-up assertion by %s does not match session user %s", asserted.email, user.email
        )
        await _audit_failure("credential belongs to a different user")
        return _failed(401)

    raw_token = request.cookies.get(manager.cookie_name)
    async with ctx.session_factory() as session:
        refreshed = await AuthRepo(session).refresh_session_last_auth(hash_token(raw_token))
    if not refreshed:
        await _audit_failure("session no longer live")
        return _failed(401)
    await emit_step_up_event(
        ctx,
        AuditEventType.AUTH_STEP_UP_SUCCEEDED,
        user_email=user.email,
        request=request,
    )
    return JSONResponse({"verified": True})


# -- management (behind the session middleware) ------------------------


@router.get("/settings/passkeys", response_class=HTMLResponse)
async def passkeys_page(request: Request):
    user = getattr(request.state, "user", None)
    credentials = []
    if user is not None:
        async with request.app.state.ctx.session_factory() as session:
            credentials = await AuthRepo(session).list_credentials_for_user(user.id)
    cfg = auth_config(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "settings_passkeys.html",
        {
            "user": user,
            "credentials": credentials,
            "auth_enabled": bool(cfg and cfg.enabled),
        },
    )


async def _owned_credential(request: Request, credential_id: str):
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    async with request.app.state.ctx.session_factory() as session:
        row = await AuthRepo(session).get_credential(credential_id)
    return row if row is not None and row.user_id == user.id else None


@router.post("/settings/passkeys/rename", dependencies=[Depends(require_step_up)])
async def rename_passkey(request: Request, credential_id: str = Form(""), name: str = Form("")):
    row = await _owned_credential(request, credential_id)
    if row is not None and name.strip():
        async with request.app.state.ctx.session_factory() as session:
            await AuthRepo(session).rename_credential(credential_id, name.strip()[:128])
    return RedirectResponse("/settings/passkeys", status_code=303)


@router.post("/settings/passkeys/delete", dependencies=[Depends(require_step_up)])
async def delete_passkey(request: Request, credential_id: str = Form("")):
    row = await _owned_credential(request, credential_id)
    if row is not None:
        async with request.app.state.ctx.session_factory() as session:
            await AuthRepo(session).delete_credential(credential_id)
        await audit_auth(
            request.app.state.ctx,
            AuditEventType.AUTH_PASSKEY_DELETED,
            email=request.state.user.email,
            credential_id=credential_id,
            **request_meta(request),
        )
    return RedirectResponse("/settings/passkeys", status_code=303)

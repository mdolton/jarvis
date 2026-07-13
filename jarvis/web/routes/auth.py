"""Auth routes: emailed one-time-code login, logout, logout-all.

The emailed code is the enrollment/recovery channel, not the authenticator
(see jarvis/auth/codes.py for the NIST framing); passkeys take over as the
daily login in a later PR.

POST /auth/login is enumeration-proof BY CONSTRUCTION: the request path does
identical work — generate code + nonce, consume rate-limit tokens, set the
nonce cookie, redirect — whether the email is on the allow-list, off it, or
rate-limited. Everything that depends on the answer (allow-list check, DB
writes, the mail send) runs as a background task after the response is on
the wire, because the mail send is the timing oracle (CVE-2026-26185).
"""

from dataclasses import dataclass, field

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from jarvis.auth.codes import LoginCodeService
from jarvis.auth.mailer import build_mailer
from jarvis.auth.ratelimit import ExponentialBackoff, RateLimiter
from jarvis.auth.sessions import SessionManager
from jarvis.core.types import AuditEventType
from jarvis.persistence.repositories import AuthRepo
from jarvis.web.auth_audit import audit_auth, request_meta
from jarvis.web.auth_middleware import LOGIN_PATH, auth_config
from jarvis.web.step_up import require_step_up

router = APIRouter()

VERIFY_PATH = "/auth/verify"

# One failure message for every verify miss — wrong code, expired, consumed,
# attempt budget spent, nonce mismatch, rate-limited. Distinct messages would
# leak which one happened.
VERIFY_FAILED_MESSAGE = (
    "That code was not accepted. It may be mistyped, expired, or already "
    "used — request a new one and try again."
)

# The same-browser binding: set when a code is requested, its hash stored on
# the code row, required at verification. The code is typed into the tab that
# requested it, so this costs no UX — but a code phished or shoulder-surfed
# out of the inbox is useless in any other browser.
NONCE_COOKIE = "jarvis_login_nonce"

# Login-code request budget (token buckets, see jarvis/auth/ratelimit.py):
# 3 codes per address and 10 per client IP per ~15 minutes, refilling
# continuously. Verify gets a per-IP budget on top of the per-code 5-attempt
# lockout. Exhausted buckets return the IDENTICAL response minus the send.
_PER_15_MIN = 1 / 900.0
LOGIN_EMAIL_LIMIT = {"capacity": 3, "refill_per_sec": 3 * _PER_15_MIN}
LOGIN_IP_LIMIT = {"capacity": 10, "refill_per_sec": 10 * _PER_15_MIN}
VERIFY_IP_LIMIT = {"capacity": 15, "refill_per_sec": 15 * _PER_15_MIN}

# On top of the buckets: repeated login FAILURES from one IP earn an
# exponentially growing lockout (3 free misses, then 1s, 2s, 4s, ... capped
# at 15 min). Shared between code verifies and passkey assertions.
LOGIN_FAILURE_BACKOFF = {"free_failures": 3, "base_delay_sec": 1.0, "max_delay_sec": 900.0}


def _default_backoff() -> ExponentialBackoff:
    return ExponentialBackoff(**LOGIN_FAILURE_BACKOFF)


@dataclass
class AuthFlow:
    """Per-app singletons for the login flow (limiters hold state)."""

    codes: LoginCodeService
    login_email_limiter: RateLimiter
    login_ip_limiter: RateLimiter
    verify_ip_limiter: RateLimiter
    login_backoff: ExponentialBackoff = field(default_factory=_default_backoff)


def _auth_flow(request: Request) -> AuthFlow:
    flow = getattr(request.app.state, "auth_flow", None)
    if flow is None:
        ctx = request.app.state.ctx
        flow = AuthFlow(
            codes=LoginCodeService(
                session_factory=ctx.session_factory,
                config=ctx.config.jarvis.auth,
                mailer=build_mailer(ctx.config.jarvis.mail),
            ),
            login_email_limiter=RateLimiter(**LOGIN_EMAIL_LIMIT),
            login_ip_limiter=RateLimiter(**LOGIN_IP_LIMIT),
            verify_ip_limiter=RateLimiter(**VERIFY_IP_LIMIT),
        )
        request.app.state.auth_flow = flow
    return flow


def _client_ip(request: Request) -> str | None:
    # Real only if uvicorn's forwarded_allow_ips trusts just the proxy.
    return request.client.host if request.client else None


def _session_manager(request: Request) -> SessionManager | None:
    cfg = auth_config(request)
    if cfg is None:
        return None
    return SessionManager(session_factory=request.app.state.ctx.session_factory, config=cfg)


def _set_nonce_cookie(request: Request, response: Response, nonce: str) -> None:
    cfg = auth_config(request)
    response.set_cookie(
        NONCE_COOKIE,
        nonce,
        max_age=(cfg.code_ttl_minutes if cfg else 10) * 60,
        path="/auth",
        secure=bool(cfg and cfg.secure_cookies),
        httponly=True,
        samesite="lax",
    )


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return request.app.state.templates.TemplateResponse(request, "auth_login.html", {})


@router.post("/auth/login")
async def request_code(request: Request, background_tasks: BackgroundTasks, email: str = Form("")):
    flow = _auth_flow(request)
    ctx = request.app.state.ctx
    meta = request_meta(request)
    login = flow.codes.start_login(email=email, ip=_client_ip(request))

    # Both buckets are spent unconditionally (no short-circuit), and a
    # rate-limited request gets the identical response — just no send.
    email_ok = flow.login_email_limiter.allow(login.email)
    ip_ok = flow.login_ip_limiter.allow(_client_ip(request) or "unknown")
    if email_ok and ip_ok:
        # The allow-list check, DB write, and mail send run AFTER the
        # response — backgrounding them is what closes the timing oracle.
        background_tasks.add_task(flow.codes.issue_and_send, login)
    else:
        scopes = [s for s, ok in (("login_email", email_ok), ("login_ip", ip_ok)) if not ok]
        background_tasks.add_task(
            audit_auth, ctx, AuditEventType.AUTH_RATE_LIMITED, scope=scopes, **meta
        )
    # Audit writes are backgrounded like the send: the request path stays
    # constant-work for on-list, off-list, and rate-limited alike.
    background_tasks.add_task(
        audit_auth,
        ctx,
        AuditEventType.AUTH_LOGIN_CODE_REQUESTED,
        email=login.email,
        rate_limited=not (email_ok and ip_ok),
        **meta,
    )

    response = RedirectResponse(VERIFY_PATH, status_code=303)
    _set_nonce_cookie(request, response, login.nonce)
    return response


@router.get("/auth/verify", response_class=HTMLResponse)
async def verify_page(request: Request):
    return _verify_response(request, error=None)


@router.post("/auth/verify")
async def verify_code(request: Request, code: str = Form("")):
    flow = _auth_flow(request)
    ctx = request.app.state.ctx
    ip = _client_ip(request) or "unknown"
    meta = request_meta(request)

    # Backoff first (a locked-out IP shouldn't spend limiter tokens), then
    # the steady-rate bucket. Either denial skips the verify entirely but
    # returns the one generic failure page.
    in_backoff = not flow.login_backoff.allowed(ip)
    rate_limited = not in_backoff and not flow.verify_ip_limiter.allow(ip)
    user_id = None
    if not in_backoff and not rate_limited:
        user_id = await flow.codes.verify(code=code, nonce=request.cookies.get(NONCE_COOKIE))
    if user_id is None:
        if in_backoff or rate_limited:
            scope = "login_backoff" if in_backoff else "verify_ip"
            await audit_auth(ctx, AuditEventType.AUTH_RATE_LIMITED, scope=scope, **meta)
        else:
            flow.login_backoff.record_failure(ip)
            await audit_auth(ctx, AuditEventType.AUTH_LOGIN_FAILED, method="code", **meta)
        return _verify_response(request, error=VERIFY_FAILED_MESSAGE)

    flow.login_backoff.reset(ip)
    await audit_auth(
        ctx, AuditEventType.AUTH_LOGIN_SUCCEEDED, method="code", user_id=str(user_id), **meta
    )
    manager = _session_manager(request)
    response = RedirectResponse("/", status_code=303)
    if manager is not None:
        raw = await manager.issue_session(user_id, request)
        manager.set_session_cookie(response, raw)
    response.delete_cookie(NONCE_COOKIE, path="/auth")
    return response


def _verify_response(request: Request, *, error: str | None) -> HTMLResponse:
    cfg = auth_config(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "auth_verify.html",
        {"error": error, "code_ttl_minutes": cfg.code_ttl_minutes if cfg else 10},
    )


@router.post("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse(LOGIN_PATH, status_code=302)
    manager = _session_manager(request)
    if manager is None:
        return response
    raw_token = request.cookies.get(manager.cookie_name)
    if raw_token:
        await manager.revoke(raw_token)
        await audit_auth(request.app.state.ctx, AuditEventType.AUTH_LOGOUT, **request_meta(request))
    manager.clear_session_cookie(response)
    return response


@router.post("/auth/logout-all", dependencies=[Depends(require_step_up)])
async def logout_all(request: Request):
    """Revoke every session for the signed-in user ("sign out all devices")."""
    response = RedirectResponse(LOGIN_PATH, status_code=302)
    manager = _session_manager(request)
    if manager is None:
        return response
    # /auth/* is exempt from the session middleware, so validate the cookie
    # here rather than reading request.state.user (always None on this path).
    raw_token = request.cookies.get(manager.cookie_name)
    user = await manager.validate(raw_token) if raw_token else None
    if user is not None:
        async with request.app.state.ctx.session_factory() as session:
            await AuthRepo(session).revoke_all_sessions_for_user(user.id)
        await audit_auth(
            request.app.state.ctx,
            AuditEventType.AUTH_SESSIONS_REVOKED,
            email=user.email,
            **request_meta(request),
        )
    manager.clear_session_cookie(response)
    return response

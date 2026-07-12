"""Deny-by-default session gate for the dashboard.

Every route requires a valid session cookie except the explicit exempt list.
Gated on config: with auth.enabled false (the default until the login flow
ships) every request passes through untouched, but request.state.user is
always set so downstream handlers can rely on it existing.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from jarvis.auth.sessions import SessionManager
from jarvis.config.schema import AuthConfig
from jarvis.persistence.repositories import AuthRepo

# The ONLY paths reachable without a session:
#   /healthz         — Docker healthcheck + monitoring
#   /events/webhook  — already Bearer-authed (secrets.compare_digest); session
#                      auth must not double-auth it or shadow its
#                      404-when-disabled behavior
#   /auth/*          — the login routes themselves
#   /static/*        — stylesheets for the login page
_EXEMPT_EXACT = {"/healthz", "/events/webhook"}
_EXEMPT_PREFIXES = ("/auth/", "/static/")

LOGIN_PATH = "/auth/login"
REGISTER_PATH = "/auth/passkey/register"


def auth_config(request: Request) -> AuthConfig | None:
    """The real AuthConfig, or None when absent (e.g. mocked test contexts)."""
    ctx = getattr(request.app.state, "ctx", None)
    cfg = getattr(getattr(getattr(ctx, "config", None), "jarvis", None), "auth", None)
    return cfg if isinstance(cfg, AuthConfig) else None


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIXES)


class SessionAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        cfg = auth_config(request)
        if cfg is None or not cfg.enabled:
            return await call_next(request)
        if _is_exempt(request.url.path):
            return await call_next(request)

        manager = SessionManager(session_factory=request.app.state.ctx.session_factory, config=cfg)
        raw_token = request.cookies.get(manager.cookie_name)
        user = await manager.validate(raw_token) if raw_token else None
        if user is not None:
            request.state.user = user
            # Mandatory enrollment: an emailed code establishes the account,
            # but NIST SP 800-63B §3.1.3.1 prohibits email as an out-of-band
            # AUTHENTICATOR, so "emailed code only" must never be a steady
            # state — a user with zero passkeys can go nowhere but the
            # enrollment page (its routes live under the /auth/ exemption)
            # until one is registered.
            async with request.app.state.ctx.session_factory() as session:
                enrolled = await AuthRepo(session).has_credentials(user.id)
            if not enrolled:
                if request.headers.get("hx-request"):
                    return PlainTextResponse(
                        "passkey enrollment required",
                        status_code=401,
                        headers={"HX-Redirect": REGISTER_PATH},
                    )
                return RedirectResponse(REGISTER_PATH, status_code=302)
            return await call_next(request)

        if request.headers.get("hx-request"):
            # A plain redirect would be swallowed into the partial swap and
            # render the login page inside a table cell; HX-Redirect makes
            # htmx do a full-page navigation instead.
            return PlainTextResponse(
                "authentication required",
                status_code=401,
                headers={"HX-Redirect": LOGIN_PATH},
            )
        return RedirectResponse(LOGIN_PATH, status_code=302)

"""CSRF synchronizer tokens for the dashboard.

NIST SP 800-63B-4 §5.1 requires POST/PUT content to carry a session-bound
value the RP verifies; origin checking (security.py) + SameSite=Lax alone is
defensible, but an agent-driving dashboard wants both layers.

The token is HMAC-SHA256(JARVIS_SECRETS_KEY, raw session token): per-session
and stateless — nothing extra is stored, and it invalidates with the session.
The page embeds it three ways (all render-time, via the Jinja context
processor in app.py):
  - a hidden `csrf_token` input in every POST form,
  - a body-level hx-headers attribute, so every hx-post/hx-delete inherits
    the `X-CSRF-Token` header without per-element annotation,
  - a `<meta name="csrf-token">` tag that passkeys.js reads for its fetches.

Verification: every unsafe-method request that carries a session cookie must
present the matching token (header first, then form field). Requests WITHOUT
a session cookie pass — there is no session to forge yet; the pre-auth login
routes are covered by the same-origin check plus the login nonce cookie.
Because the session cookie is HttpOnly, a token can only be minted server-side
into a same-origin page; a cross-site attacker holds neither.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import ClassVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

CSRF_HEADER = "x-csrf-token"
CSRF_FORM_FIELD = "csrf_token"

# Both spellings of the session cookie (secure_cookies toggles the prefix);
# the middleware can't reach config in mocked contexts, so it checks both.
_SESSION_COOKIES = ("__Host-jarvis_session", "jarvis_session")


def csrf_token_for_session(raw_session_token: str) -> str | None:
    """The synchronizer token for a session, or None if the key is unset."""
    key = os.environ.get("JARVIS_SECRETS_KEY")
    if not key:
        return None
    return hmac.new(key.encode(), raw_session_token.encode(), hashlib.sha256).hexdigest()


def session_cookie_value(request: Request) -> str | None:
    for name in _SESSION_COOKIES:
        value = request.cookies.get(name)
        if value:
            return value
    return None


class CSRFTokenMiddleware(BaseHTTPMiddleware):
    """Reject unsafe-method requests whose session lacks its CSRF token."""

    _UNSAFE_METHODS: ClassVar[set[str]] = {"POST", "PUT", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, call_next):
        if request.method.upper() in self._UNSAFE_METHODS:
            raw_session = session_cookie_value(request)
            if raw_session is not None:
                expected = csrf_token_for_session(raw_session)
                if expected is None:
                    # No JARVIS_SECRETS_KEY: fail CLOSED — a session exists,
                    # so tokens were supposed to be mintable.
                    return PlainTextResponse(
                        "CSRF verification unavailable (JARVIS_SECRETS_KEY unset)",
                        status_code=403,
                    )
                supplied = await self._supplied_token(request)
                if supplied is None or not hmac.compare_digest(supplied, expected):
                    return PlainTextResponse("CSRF token missing or invalid", status_code=403)
        return await call_next(request)

    @staticmethod
    async def _supplied_token(request: Request) -> str | None:
        header = request.headers.get(CSRF_HEADER)
        if header:
            return header
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            # body() BEFORE form(): form() alone consumes the stream without
            # caching request._body, and starlette's BaseHTTPMiddleware only
            # replays a CACHED body downstream — skip the body() call and
            # every handler after this middleware sees an empty form (the
            # step-up replay page then echoes zero fields).
            await request.body()
            form = await request.form()
            value = form.get(CSRF_FORM_FIELD)
            return value if isinstance(value, str) and value else None
        return None

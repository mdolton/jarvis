"""Dashboard request safety helpers."""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse


class SameOriginUnsafeMethodMiddleware(BaseHTTPMiddleware):
    """Reject browser unsafe-method requests from a different origin.

    Deny-by-default: an unsafe-method request must prove same-origin via its
    Origin (or Referer) header. Non-browser callers that legitimately send
    neither must be explicitly exempted below.
    """

    _UNSAFE_METHODS: ClassVar[set[str]] = {"POST", "PUT", "PATCH", "DELETE"}
    # /events/webhook is a machine endpoint authenticated by a Bearer token
    # (secrets.compare_digest); its callers (curl, automations) legitimately
    # send no Origin/Referer, so it skips the headers-absent denial below. A
    # request that DOES carry a foreign Origin is still blocked — that shape
    # only comes from a browser, and no legitimate browser flow posts here.
    _HEADERLESS_OK_PATHS: ClassVar[set[str]] = {"/events/webhook"}

    async def dispatch(self, request: Request, call_next):
        if request.method.upper() in self._UNSAFE_METHODS:
            headerless = not (request.headers.get("origin") or request.headers.get("referer"))
            exempt = headerless and request.url.path in self._HEADERLESS_OK_PATHS
            if not exempt and not _is_same_origin(request):
                return PlainTextResponse("cross-origin unsafe request blocked", status_code=403)
        return await call_next(request)


def _is_same_origin(request: Request) -> bool:
    host = request.headers.get("host")
    if not host:
        return False

    origin = request.headers.get("origin")
    if origin:
        return _header_host_matches(origin, host)

    referer = request.headers.get("referer")
    if referer:
        return _header_host_matches(referer, host)

    # Neither header present: deny. Browsers always send Origin on unsafe
    # methods, so a header-less POST is a non-browser caller — those must be
    # explicitly exempted, not waved through. (Failing open here was the
    # pre-auth CSRF hole: any cross-context request that stripped both
    # headers bypassed the check entirely.)
    return False


def _header_host_matches(value: str, host: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.netloc.lower() == host.lower()

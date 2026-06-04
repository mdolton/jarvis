"""Dashboard request safety helpers."""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse


class SameOriginUnsafeMethodMiddleware(BaseHTTPMiddleware):
    """Reject browser unsafe-method requests from a different origin.

    The dashboard is designed for operator use behind a trusted deployment
    boundary. This check prevents cross-site form posts from releasing
    side-effecting actions when a browser includes Origin or Referer headers.
    """

    _UNSAFE_METHODS: ClassVar[set[str]] = {"POST", "PUT", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, call_next):
        if request.method.upper() in self._UNSAFE_METHODS and not _is_same_origin(request):
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

    return True


def _header_host_matches(value: str, host: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.netloc.lower() == host.lower()

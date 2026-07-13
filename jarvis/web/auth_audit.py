"""Audit trail for the auth surface: login attempts, code requests, passkey
ceremonies, step-up, logout, rate-limit trips, session revocations — each
with the client IP and user agent.

Events are written straight through AuditRepo (the codes.py precedent)
rather than ctx.audit.emit: the buffered AuditLogger is only started by the
full bootstrap, while these routes must also audit under partially-mocked
test contexts that carry just a real session_factory. Same table either way,
so the dashboard's SSE tail (which polls the table) sees both.

The IP is only as real as the proxy config: request.client.host is the
socket peer unless uvicorn's forwarded_allow_ips trusts the reverse proxy —
see jarvis.forwarded_allow_ips.
"""

from __future__ import annotations

import logging

from starlette.requests import Request

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.repositories import AuditRepo

logger = logging.getLogger(__name__)


def request_meta(request: Request) -> dict:
    """The {ip, user_agent} pair stamped onto every auth audit event.

    Computed eagerly (not inside the background task) so the values are
    pinned even if the request scope is recycled after the response.
    """
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


async def audit_auth(ctx, type_: AuditEventType, **payload) -> None:
    """Write one auth audit event; never let an audit failure break auth."""
    factory = getattr(ctx, "session_factory", None)
    if factory is None:
        return
    try:
        async with factory() as session:
            await AuditRepo(session).write_many([AuditEvent(type=type_, payload=payload)])
    except Exception:
        logger.exception("auth audit write failed (event type %s)", type_)

"""GET /healthz — JSON health check."""

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text

_log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request):
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return {"status": "ok", "detail": "no app context (startup)"}

    # Check DB writability.
    db_status = "ok"
    try:
        async with ctx.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        _log.exception("healthz: DB check failed")
        db_status = "error"

    # Check MCP servers.
    mcp_count = len(ctx.mcp_manager.agent_mcp_servers())

    status = "ok" if db_status == "ok" else "degraded"
    return {
        "status": status,
        "db": db_status,
        "mcp_servers": mcp_count,
    }

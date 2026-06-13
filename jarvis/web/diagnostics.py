"""Shared dashboard and health-check diagnostics."""

import logging
from typing import Any
from unittest.mock import Mock

from sqlalchemy import text

from jarvis.core.types import AuditEventType
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.repositories import AuditRepo

_log = logging.getLogger(__name__)


async def collect_diagnostics(ctx) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}

    components["db"] = await _db_status(ctx)
    components["mcp"] = _mcp_status(ctx)
    components["scheduler"] = _scheduler_status(ctx)
    components["discord"] = _discord_status(ctx)
    components["models"] = await _model_status(ctx)
    components["oauth"] = await _oauth_status(ctx)
    components["audit"] = await _audit_status(ctx)

    status = "degraded" if any(c["status"] == "error" for c in components.values()) else "ok"

    return {"status": status, "components": components}


async def _db_status(ctx) -> dict[str, Any]:
    try:
        async with ctx.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        _log.exception("diagnostics: DB check failed")
        return {"status": "error"}


def _mcp_status(ctx) -> dict[str, Any]:
    try:
        connected = len(ctx.mcp_manager.agent_mcp_servers())
        return {"status": "ok", "connected": connected}
    except Exception:
        _log.exception("diagnostics: MCP check failed")
        return {"status": "error", "connected": 0}


def _scheduler_status(ctx) -> dict[str, Any]:
    try:
        return {"status": "ok", "active_jobs": ctx.scheduler.active_job_count()}
    except Exception:
        _log.exception("diagnostics: scheduler check failed")
        return {"status": "error", "active_jobs": 0}


def _discord_status(ctx) -> dict[str, Any]:
    adapters = [a for a in getattr(ctx, "channel_adapters", []) if getattr(a, "kind", "") == "discord"]
    if not adapters:
        return {"status": "warn", "configured": False, "ready": False}
    adapter = adapters[0]
    ready_fn = getattr(adapter, "is_ready", None)
    ready = bool(ready_fn()) if callable(ready_fn) else True
    return {"status": "ok" if ready else "warn", "configured": True, "ready": ready}


async def _model_status(ctx) -> dict[str, Any]:
    if _maybe_mock_missing(ctx, "model_catalog"):
        return {"status": "unknown", "count": 0, "models": []}
    try:
        catalog = await ctx.model_catalog.list_models()
        return {
            "status": "ok" if catalog.ok else "warn",
            "count": len(catalog.models),
            "models": catalog.models[:10],
        }
    except Exception:
        _log.exception("diagnostics: model catalog check failed")
        return {"status": "error", "count": 0, "models": []}


async def _oauth_status(ctx) -> dict[str, Any]:
    if _maybe_mock_missing(ctx, "oauth_flow"):
        return {"status": "unknown", "connected": 0, "needs_reauth": 0}
    try:
        async with ctx.session_factory() as session:
            rows = await MCPConnectionRepo(session).list_all()
        needs_reauth = sum(1 for row in rows if row.status == "needs_reauth")
        return {
            "status": "warn" if needs_reauth else "ok",
            "connected": len(rows),
            "needs_reauth": needs_reauth,
        }
    except Exception:
        _log.exception("diagnostics: OAuth check failed")
        return {"status": "unknown", "connected": 0, "needs_reauth": 0}


async def _audit_status(ctx) -> dict[str, Any]:
    if _maybe_mock_missing(ctx, "audit"):
        return {"status": "unknown", "recent_errors": 0}
    error_types = [
        AuditEventType.LLM_ERROR,
        AuditEventType.TOOL_ERROR,
        AuditEventType.CONFIG_RELOAD_FAILED,
        AuditEventType.OAUTH_DISCOVERY_FAILED,
        AuditEventType.OAUTH_REFRESH_PERMANENTLY_FAILED,
    ]
    try:
        async with ctx.session_factory() as session:
            rows = await AuditRepo(session).recent(types=error_types, limit=5)
        return {"status": "warn" if rows else "ok", "recent_errors": len(rows)}
    except Exception:
        _log.exception("diagnostics: audit check failed")
        return {"status": "unknown", "recent_errors": 0}


def _maybe_mock_missing(ctx, name: str) -> bool:
    if not isinstance(ctx, Mock):
        return False
    if name in getattr(ctx, "__dict__", {}):
        return False
    return name not in getattr(ctx, "_mock_children", {})

"""GET /healthz — JSON health check."""

from fastapi import APIRouter, Request

from jarvis.web.diagnostics import collect_diagnostics

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request):
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return {"status": "ok", "detail": "no app context (startup)"}

    diagnostics = await collect_diagnostics(ctx)
    components = diagnostics["components"]
    return {
        "status": diagnostics["status"],
        "db": components["db"]["status"],
        "mcp_servers": components["mcp"]["connected"],
        "components": components,
    }

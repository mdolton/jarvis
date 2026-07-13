"""GET /mcp — MCP server list + tools, plus OAuth Providers section."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo, SettingsRepo
from jarvis.web.step_up import require_step_up

router = APIRouter()


@router.get("/mcp", response_class=HTMLResponse)
async def mcp_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        providers = await MCPProviderRepo(session).list_all()
        connections = await MCPConnectionRepo(session).list_all()
        servers = await MCPServerRepo(session).list_all()
        server_tools = {
            srv.id: await MCPToolRepo(session).list_for_server(srv.id) for srv in servers
        }
        disabled = set(await SettingsRepo(session).get("mcp.stdio_disabled") or [])
    runtime_by_name = {s.name: s for s in servers}
    conns_by_provider: dict[str, list] = {}
    for c in connections:
        rt = runtime_by_name.get(c.runtime_name)
        conns_by_provider.setdefault(c.provider_key, []).append(
            {
                "id": str(c.id),
                "label": c.label,
                "runtime_name": c.runtime_name,
                "enabled": c.enabled,
                "auth_status": c.status,
                "last_error": c.last_error,
                "authorized": c.access_token_enc is not None,
                "runtime_status": rt.status if rt else "disconnected",
                "tools": server_tools.get(rt.id, []) if rt else [],
            }
        )
    providers_view = [
        {
            "key": p.key,
            "display_name": p.display_name,
            "kind": p.kind,
            "builtin": p.builtin,
            "auth_mode": p.auth_mode,
            "mcp_url": p.mcp_url,
            "default_scopes": p.default_scopes or [],
            "connections": conns_by_provider.get(p.key, []),
        }
        for p in providers
    ]
    stdio_servers = [
        {
            "name": s.name,
            "status": s.status,
            "last_error": s.last_error,
            "enabled": s.name not in disabled,
            "tools": server_tools.get(s.id, []),
        }
        for s in servers
        if s.source == "stdio"
    ]
    return templates.TemplateResponse(
        request,
        "mcp.html",
        {
            "providers": providers_view,
            "stdio_servers": stdio_servers,
            "server_tools": server_tools,
        },
    )


@router.post("/mcp/tools/{tool_id}/policy", dependencies=[Depends(require_step_up)])
async def set_tool_policy(
    request: Request,
    tool_id: UUID,
    policy_override: str = Form(""),
):
    ctx = request.app.state.ctx
    policy = policy_override.strip() or None
    if policy not in {None, "allow", "deny", "confirm"}:
        raise HTTPException(status_code=400, detail="invalid policy override")
    async with ctx.session_factory() as session:
        await MCPToolRepo(session).set_policy_override(tool_id, policy)
    mcp_manager = getattr(ctx, "mcp_manager", None)
    clear_policy_cache = getattr(mcp_manager, "clear_policy_cache", None)
    if callable(clear_policy_cache):
        clear_policy_cache()
    return RedirectResponse(url="/mcp", status_code=303)

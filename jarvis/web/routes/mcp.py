"""GET /mcp — MCP server list + tools, plus OAuth Providers section."""

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.store import OAuthCredentialsRepo
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo

router = APIRouter()


@router.get("/mcp", response_class=HTMLResponse)
async def mcp_page(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates

    async with ctx.session_factory() as session:
        servers = await MCPServerRepo(session).list_all()
        server_tools = {}
        for srv in servers:
            server_tools[srv.id] = await MCPToolRepo(session).list_for_server(srv.id)
        creds_by_key = {r.provider_key: r for r in await OAuthCredentialsRepo(session).list_all()}

    oauth_cards = []
    for key, entry in OAUTH_CATALOG.items():
        cred = creds_by_key.get(key)
        if cred is None or not cred.access_token_enc:
            state = "disconnected"
        elif cred.status == "needs_reauth":
            state = "needs_reauth"
        else:
            state = "connected"
        oauth_cards.append(
            {
                "key": key,
                "display_name": entry.display_name,
                "state": state,
                "last_error": cred.last_error if cred else None,
                "updated_at": cred.updated_at if cred else None,
            }
        )

    return templates.TemplateResponse(
        request,
        "mcp.html",
        {"servers": servers, "server_tools": server_tools, "oauth_cards": oauth_cards},
    )


@router.post("/mcp/tools/{tool_id}/policy")
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

"""GET /mcp — MCP server list + tools, plus OAuth Providers section."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

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
        creds_by_key = {
            r.provider_key: r
            for r in await OAuthCredentialsRepo(session).list_all()
        }

    oauth_cards = []
    for key, entry in OAUTH_CATALOG.items():
        cred = creds_by_key.get(key)
        if cred is None or not cred.access_token_enc:
            state = "disconnected"
        elif cred.status == "needs_reauth":
            state = "needs_reauth"
        else:
            state = "connected"
        oauth_cards.append({
            "key": key,
            "display_name": entry.display_name,
            "state": state,
            "last_error": cred.last_error if cred else None,
            "updated_at": cred.updated_at if cred else None,
        })

    return templates.TemplateResponse(
        request,
        "mcp.html",
        {"servers": servers, "server_tools": server_tools, "oauth_cards": oauth_cards},
    )

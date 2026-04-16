"""GET /mcp — MCP server list + tools."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

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

    return templates.TemplateResponse(
        request,
        "mcp.html",
        {"servers": servers, "server_tools": server_tools},
    )

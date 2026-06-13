"""Provider / connection / stdio mutation endpoints for the MCP tab."""
import json
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.oauth.catalog import unique_runtime_name
from jarvis.oauth.crypto import encrypt_blob
from jarvis.oauth.store import MCPConnectionRepo

router = APIRouter()


def _redirect():
    return RedirectResponse(url="/mcp", status_code=303)


async def _emit(ctx, action: str, **payload):
    emit = getattr(getattr(ctx, "audit", None), "emit", None)
    if emit is not None:
        await emit(AuditEvent(type=AuditEventType.MCP_CONFIG_CHANGED,
                              payload={"action": action, **payload}))


@router.post("/mcp/connections/add")
async def add_connection(
    request: Request,
    provider_key: str = Form(...),
    label: str = Form(...),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    scopes: str = Form(""),
    url_override: str = Form(""),
    headers: str = Form(""),  # newline-separated "Name: value"
):
    ctx = request.app.state.ctx
    try:
        entry = await ctx.catalog.get(provider_key)
    except KeyError:
        raise HTTPException(404, f"unknown provider {provider_key!r}")
    async with ctx.session_factory() as session:
        existing = {c.runtime_name for c in await MCPConnectionRepo(session).list_all()}
    rt = unique_runtime_name(existing, provider_key, label)

    key = ctx.config.secrets_key
    cid_enc = encrypt_blob(client_id.encode(), key) if client_id.strip() else None
    sec_enc = encrypt_blob(client_secret.encode(), key) if client_secret.strip() else None
    scope_list = scopes.split() if scopes.strip() else list(entry.default_scopes)
    headers_enc = None
    if entry.kind in ("http", "sse") and headers.strip():
        parsed = {}
        for line in headers.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                parsed[k.strip()] = v.strip()
        headers_enc = encrypt_blob(json.dumps(parsed).encode(), key)

    async with ctx.session_factory() as session:
        conn = await MCPConnectionRepo(session).create(
            provider_key=provider_key, label=label.strip() or "Default", runtime_name=rt,
            client_id_enc=cid_enc, client_secret_enc=sec_enc, scopes=scope_list,
            url_override=url_override.strip() or None, headers_enc=headers_enc)
        conn_id = conn.id
    await _emit(ctx, "connection.add", provider_key=provider_key, runtime_name=rt)

    if entry.kind in ("http", "sse"):
        async with ctx.session_factory() as session:
            conn = await MCPConnectionRepo(session).get(conn_id)
        try:
            await ctx.mcp_manager.connect_connection(conn)
        except Exception:
            pass  # failure is recorded as server status by the manager
    return _redirect()


@router.post("/mcp/connections/{connection_id}/enable")
async def enable_connection(request: Request, connection_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = MCPConnectionRepo(session)
        await repo.set_enabled(connection_id, enabled=True)
        conn = await repo.get(connection_id)
    if conn is None:
        raise HTTPException(404)
    try:
        await ctx.mcp_manager.connect_connection(conn)
    except Exception:
        pass
    await _emit(ctx, "connection.enable", runtime_name=conn.runtime_name)
    return _redirect()


@router.post("/mcp/connections/{connection_id}/disable")
async def disable_connection(request: Request, connection_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = MCPConnectionRepo(session)
        conn = await repo.get(connection_id)
        if conn is None:
            raise HTTPException(404)
        await repo.set_enabled(connection_id, enabled=False)
    await ctx.mcp_manager.disconnect(conn.runtime_name)
    await _emit(ctx, "connection.disable", runtime_name=conn.runtime_name)
    return _redirect()


@router.post("/mcp/connections/{connection_id}/remove")
async def remove_connection(request: Request, connection_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        conn = await MCPConnectionRepo(session).get(connection_id)
        if conn is None:
            raise HTTPException(404)
        runtime_name = conn.runtime_name
        had_token = conn.access_token_enc is not None
    await ctx.mcp_manager.disconnect(runtime_name)
    flow = getattr(ctx, "oauth_flow", None)
    if flow is not None and had_token:
        try:
            await flow.revoke(connection_id)
        except Exception:
            pass
    async with ctx.session_factory() as session:
        await MCPConnectionRepo(session).delete(connection_id)
    await _emit(ctx, "connection.remove", runtime_name=runtime_name)
    return _redirect()

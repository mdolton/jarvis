"""Provider / connection / stdio mutation endpoints for the MCP tab."""
import json
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.oauth.catalog import unique_runtime_name
from jarvis.oauth.crypto import encrypt_blob
from jarvis.oauth.discovery import DiscoveryResult, discover_provider
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo, SettingsRepo

router = APIRouter()

_STDIO_DISABLED_KEY = "mcp.stdio_disabled"


async def _set_stdio_disabled(ctx, name: str, disabled: bool) -> None:
    async with ctx.session_factory() as session:
        repo = SettingsRepo(session)
        current = set(await repo.get(_STDIO_DISABLED_KEY) or [])
        if disabled:
            current.add(name)
        else:
            current.discard(name)
        await repo.set(_STDIO_DISABLED_KEY, sorted(current))


def _redirect():
    return RedirectResponse(url="/mcp", status_code=303)


async def _emit(ctx, action: str, **payload):
    emit = getattr(getattr(ctx, "audit", None), "emit", None)
    if emit is not None:
        await emit(AuditEvent(type=AuditEventType.MCP_CONFIG_CHANGED,
                              payload={"action": action, **payload}))


@router.post("/mcp/providers/add")
async def add_provider(
    request: Request,
    key: str = Form(...),
    display_name: str = Form(...),
    kind: str = Form(...),            # 'oauth' | 'http' | 'sse'
    mcp_url: str = Form(...),
    auth_mode: str = Form("dcr"),
    oauth_metadata_url: str = Form(""),
    default_scopes: str = Form(""),
    header_names: str = Form(""),
):
    ctx = request.app.state.ctx
    if kind not in ("oauth", "http", "sse"):
        raise HTTPException(400, "kind must be oauth, http, or sse (stdio is file-managed)")
    key = key.strip()
    if not key:
        raise HTTPException(400, "key required")
    async with ctx.session_factory() as session:
        repo = MCPProviderRepo(session)
        if await repo.get(key) is not None:
            raise HTTPException(400, f"provider {key!r} already exists")
        await repo.upsert(
            key=key, display_name=display_name.strip(), kind=kind, mcp_url=mcp_url.strip(),
            builtin=False,
            auth_mode=(auth_mode if kind == "oauth" else None),
            oauth_metadata_url=(oauth_metadata_url.strip() or None) if kind == "oauth" else None,
            pkce=True, send_resource_indicator=True, extra_auth_params={},
            default_scopes=default_scopes.split() if default_scopes.strip() else [],
            header_names=[h.strip() for h in header_names.split(",") if h.strip()],
        )
    await _emit(ctx, "provider.add", provider_key=key, kind=kind)
    return _redirect()


@router.post("/mcp/providers/discover", response_class=HTMLResponse)
async def discover_provider_endpoint(request: Request, mcp_url: str = Form(...)):
    """Probe an MCP URL for OAuth metadata; return an HTMX fragment that prefills
    the Add Provider form. Never fails the request — discovery is best-effort."""
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    if not mcp_url.strip():
        result = DiscoveryResult(notes=["No URL provided."])
    else:
        result = await discover_provider(mcp_url, ctx.oauth_http)
    return templates.TemplateResponse(request, "_provider_discovery.html", {"r": result})


@router.post("/mcp/providers/{provider_key}/edit-credentials")
async def edit_provider_credentials(
    request: Request, provider_key: str,
    client_id: str = Form(...),
    client_secret: str = Form(""),
):
    """Set OAuth app credentials on this provider's connections (creds live on connections)."""
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        crepo = MCPConnectionRepo(session)
        conns = await crepo.list_for_provider(provider_key)
        if not conns:
            raise HTTPException(
                400,
                "provider has no connections — add a connection first, then set credentials",
            )
        key = ctx.config.secrets_key
        cid_enc = encrypt_blob(client_id.encode(), key)
        sec_enc = encrypt_blob(client_secret.encode(), key) if client_secret.strip() else None
        for c in conns:
            await crepo.set_client(c.id, client_id_enc=cid_enc, client_secret_enc=sec_enc)
    await _emit(ctx, "provider.edit_credentials", provider_key=provider_key, count=len(conns))
    return _redirect()


@router.post("/mcp/providers/{provider_key}/remove")
async def remove_provider(request: Request, provider_key: str):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = MCPProviderRepo(session)
        prov = await repo.get(provider_key)
        if prov is None:
            raise HTTPException(404)
        if prov.builtin:
            raise HTTPException(400, "built-in providers cannot be removed")
        if await repo.has_connections(provider_key):
            raise HTTPException(400, "remove its connections first")
        await repo.delete(provider_key)
    await _emit(ctx, "provider.remove", provider_key=provider_key)
    return _redirect()


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
        raise HTTPException(404, f"unknown provider {provider_key!r}") from None
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
    try:
        await ctx.mcp_manager.disconnect(conn.runtime_name)
    except Exception:
        pass
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


@router.post("/mcp/stdio/{name}/disable")
async def disable_stdio(request: Request, name: str):
    ctx = request.app.state.ctx
    await _set_stdio_disabled(ctx, name, True)
    try:
        await ctx.mcp_manager.disconnect(name)
    except Exception:
        pass
    await _emit(ctx, "stdio.disable", name=name)
    return _redirect()


@router.post("/mcp/stdio/{name}/enable")
async def enable_stdio(request: Request, name: str):
    ctx = request.app.state.ctx
    await _set_stdio_disabled(ctx, name, False)
    cfg = next((s for s in ctx.config.mcp_servers.servers if s.name == name), None)
    if cfg is not None:
        try:
            await ctx.mcp_manager.connect_server(cfg)
        except Exception:
            pass
    await _emit(ctx, "stdio.enable", name=name)
    return _redirect()


@router.post("/mcp/stdio/{name}/tools/allow-all")
async def allow_all_stdio_tools(request: Request, name: str):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        servers = await MCPServerRepo(session).list_all()
        row = next(
            (s for s in servers if s.name == name and s.source == "stdio"), None
        )
        if row is None:
            raise HTTPException(404, "stdio server not found")
        await MCPToolRepo(session).set_policy_override_for_server(row.id, "allow")
    mcp_manager = getattr(ctx, "mcp_manager", None)
    clear_policy_cache = getattr(mcp_manager, "clear_policy_cache", None)
    if callable(clear_policy_cache):
        clear_policy_cache(name)
    await _emit(ctx, "stdio.tools.allow_all", name=name)
    return _redirect()

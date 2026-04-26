"""OAuth connect / callback / disconnect routes."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.flow import OAuthCallbackError, OAuthDiscoveryError

router = APIRouter(prefix="/oauth")
_log = logging.getLogger(__name__)


@router.get("/callback")
async def oauth_callback(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    qp = request.query_params
    error = qp.get("error")

    if error is not None:
        # Best-effort: sweep any matching pending row but don't fail if absent.
        state = qp.get("state")
        if state:
            try:
                from jarvis.oauth.store import OAuthPendingRepo

                async with ctx.session_factory() as session:
                    await OAuthPendingRepo(session).delete(state)
            except Exception:
                _log.exception("failed to sweep pending row on declined callback")
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "declined", "provider": "", "message": error},
        )

    state = qp.get("state")
    code = qp.get("code")
    if not state or not code:
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "error", "provider": "", "message": "missing state or code"},
            status_code=400,
        )

    try:
        result = await ctx.oauth_flow.handle_callback(state=state, code=code)
    except OAuthCallbackError as e:
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "error", "provider": "", "message": str(e)},
            status_code=400,
        )

    # Attach the SDK server with fresh headers.
    headers = await ctx.oauth_flow.current_headers(result.provider_key)
    entry = OAUTH_CATALOG[result.provider_key]
    if ctx.mcp_manager is not None:
        try:
            await ctx.mcp_manager.replace_oauth_server(
                result.provider_key, url=entry.mcp_url, headers=headers
            )
        except Exception as e:
            _log.exception("post-callback MCP attach failed for %s", result.provider_key)
            return templates.TemplateResponse(
                request,
                "oauth_callback.html",
                {
                    "outcome": "error",
                    "provider": result.provider_key,
                    "message": f"connected, but MCP attach failed: {e}",
                },
                status_code=500,
            )

    return templates.TemplateResponse(
        request,
        "oauth_callback.html",
        {"outcome": "success", "provider": entry.display_name, "message": ""},
    )


@router.get("/connect/{provider}")
async def oauth_connect(provider: str, request: Request):
    if provider not in OAUTH_CATALOG:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    ctx = request.app.state.ctx
    try:
        consent_url = await ctx.oauth_flow.start_authorization(provider)
    except OAuthDiscoveryError as e:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "error", "message": str(e), "provider": provider},
            status_code=502,
        )
    return RedirectResponse(consent_url, status_code=302)


@router.post("/disconnect/{provider}")
async def oauth_disconnect(provider: str, request: Request):
    if provider not in OAUTH_CATALOG:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    ctx = request.app.state.ctx
    if ctx.mcp_manager is not None:
        await ctx.mcp_manager.remove_oauth_server(provider)
    await ctx.oauth_flow.revoke(provider)
    return RedirectResponse("/mcp", status_code=303)

"""OAuth connect / callback / disconnect routes."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.flow import OAuthCallbackError, OAuthDiscoveryError

router = APIRouter(prefix="/oauth")
_log = logging.getLogger(__name__)
POST_CALLBACK_ATTACH_TIMEOUT = 10.0


@router.get("/callback")
async def oauth_callback(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    qp = request.query_params
    error = qp.get("error")

    if error is not None:
        error_description = qp.get("error_description", "")
        _log.warning(
            "oauth callback returned error=%r description=%r", error, error_description
        )
        # Best-effort: sweep any matching pending row but don't fail if absent.
        state = qp.get("state")
        if state:
            try:
                from jarvis.oauth.store import OAuthPendingRepo

                async with ctx.session_factory() as session:
                    await OAuthPendingRepo(session).delete(state)
            except Exception:
                _log.exception("failed to sweep pending row on errored callback")
        # Only access_denied means the user explicitly declined. Everything else
        # (invalid_scope, invalid_request, server_error, ...) is a real error
        # we should surface so the user can debug or report it.
        outcome = "declined" if error == "access_denied" else "error"
        message = f"{error}: {error_description}" if error_description else error
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": outcome, "provider": "", "message": message},
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
            await asyncio.wait_for(
                ctx.mcp_manager.replace_oauth_server(
                    result.provider_key, url=entry.mcp_url, headers=headers
                ),
                timeout=POST_CALLBACK_ATTACH_TIMEOUT,
            )
        except Exception as e:
            _log.exception("post-callback MCP attach failed for %s", result.provider_key)
            # Tokens are stored but the server never came up. Don't leave the card
            # claiming "connected" with no tools — flag it so the dashboard tells
            # the truth and the user can retry.
            from jarvis.oauth.store import OAuthCredentialsRepo

            async with ctx.session_factory() as session:
                await OAuthCredentialsRepo(session).set_status(
                    result.provider_key,
                    status="needs_reauth",
                    last_error=f"MCP attach failed: {e}",
                )
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
        # The user clicked Disconnect — always honor it locally. A slow or failing
        # MCP teardown (e.g. an unresponsive remote) must not block revocation,
        # so bound it and fall through to revoke regardless of the outcome.
        try:
            await asyncio.wait_for(ctx.mcp_manager.remove_oauth_server(provider), timeout=10.0)
        except Exception:
            _log.exception("MCP teardown failed during disconnect of %s; revoking anyway", provider)
    await ctx.oauth_flow.revoke(provider)
    return RedirectResponse("/mcp", status_code=303)

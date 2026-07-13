"""OAuth connect / callback / disconnect routes (keyed on connection_id)."""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jarvis.oauth.flow import DCRUnsupportedError, OAuthCallbackError, OAuthDiscoveryError
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.web.step_up import require_step_up

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
        _log.warning("oauth callback returned error=%r description=%r", error, error_description)
        # Best-effort: sweep any matching pending row but don't fail if absent.
        state = qp.get("state")
        if state:
            try:
                from jarvis.oauth.store import MCPPendingRepo

                async with ctx.session_factory() as session:
                    await MCPPendingRepo(session).delete(state)
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

    entry = await ctx.catalog.get(result.provider_key)
    async with ctx.session_factory() as session:
        conn = await MCPConnectionRepo(session).get(result.connection_id)
    if conn is None:
        # The connection row vanished between token exchange and attach
        # (concurrent removal). Don't claim success for a connection that no
        # longer exists.
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {
                "outcome": "error",
                "provider": entry.display_name,
                "message": "connection was removed before the MCP attach could run",
            },
            status_code=500,
        )
    if ctx.mcp_manager is not None:
        try:
            # connect_connection resolves url_override/token from the row itself,
            # so this route no longer hand-rolls the attach.
            await asyncio.wait_for(
                ctx.mcp_manager.connect_connection(conn),
                timeout=POST_CALLBACK_ATTACH_TIMEOUT,
            )
        except TimeoutError:
            # The attach command is queued on the MCP lifecycle task and keeps
            # running after this wait gives up — a timeout is "still in
            # progress", never a failure. Do not touch connection status; the
            # dashboard's runtime status reflects the eventual outcome.
            _log.warning("post-callback MCP attach still pending for %s", result.runtime_name)
            return templates.TemplateResponse(
                request,
                "oauth_callback.html",
                {"outcome": "pending", "provider": entry.display_name, "message": ""},
            )
        except Exception as e:
            _log.exception("post-callback MCP attach failed for %s", result.runtime_name)
            # Tokens are stored but the server never came up. Don't leave the
            # connection claiming "connected" with no tools — flag it so the
            # dashboard tells the truth and the user can retry.
            async with ctx.session_factory() as session:
                await MCPConnectionRepo(session).set_status(
                    result.connection_id,
                    status="needs_reauth",
                    last_error=f"MCP attach failed: {e}",
                )
            return templates.TemplateResponse(
                request,
                "oauth_callback.html",
                {
                    "outcome": "error",
                    "provider": entry.display_name,
                    "message": f"connected, but MCP attach failed: {e}",
                },
                status_code=500,
            )

    return templates.TemplateResponse(
        request,
        "oauth_callback.html",
        {"outcome": "success", "provider": entry.display_name, "message": ""},
    )


# /callback is deliberately NOT step-up gated: it arrives as a cross-site
# top-level redirect from the provider and only completes a flow whose start
# (/connect) already demanded a fresh assertion moments earlier.
@router.get("/connect/{connection_id}", dependencies=[Depends(require_step_up)])
async def oauth_connect(connection_id: str, request: Request):
    try:
        cid = UUID(connection_id)
    except ValueError:
        raise HTTPException(
            status_code=404, detail=f"unknown connection {connection_id!r}"
        ) from None
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        conn = await MCPConnectionRepo(session).get(cid)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"unknown connection {connection_id!r}")
    try:
        consent_url = await ctx.oauth_flow.start_authorization(cid)
    except (OAuthDiscoveryError, DCRUnsupportedError) as e:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "oauth_callback.html",
            {"outcome": "error", "message": str(e), "provider": conn.provider_key},
            status_code=502,
        )
    return RedirectResponse(consent_url, status_code=302)


@router.post("/disconnect/{connection_id}", dependencies=[Depends(require_step_up)])
async def oauth_disconnect(connection_id: str, request: Request):
    try:
        cid = UUID(connection_id)
    except ValueError:
        raise HTTPException(
            status_code=404, detail=f"unknown connection {connection_id!r}"
        ) from None
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        conn = await MCPConnectionRepo(session).get(cid)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"unknown connection {connection_id!r}")
    if ctx.mcp_manager is not None:
        # The user clicked Disconnect — always honor it locally. A slow or failing
        # MCP teardown (e.g. an unresponsive remote) must not block revocation,
        # so bound it and fall through to revoke regardless of the outcome.
        try:
            await asyncio.wait_for(
                ctx.mcp_manager.remove_oauth_server(conn.runtime_name), timeout=10.0
            )
        except Exception:
            _log.exception(
                "MCP teardown failed during disconnect of %s; revoking anyway",
                conn.runtime_name,
            )
    try:
        await ctx.oauth_flow.revoke(cid)
    except Exception:
        _log.exception("revoke failed during disconnect of %s; clearing anyway", cid)
    async with ctx.session_factory() as session:
        await MCPConnectionRepo(session).clear_tokens(cid)
    return RedirectResponse("/mcp", status_code=303)

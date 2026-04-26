"""OAuth connect / callback / disconnect routes."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from jarvis.oauth.catalog import OAUTH_CATALOG
from jarvis.oauth.flow import OAuthDiscoveryError

router = APIRouter(prefix="/oauth")
_log = logging.getLogger(__name__)


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

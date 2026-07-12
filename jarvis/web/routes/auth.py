"""Auth routes: login page placeholder + logout.

The emailed-code login flow ships in the next PR; until then this exists so
the middleware's /auth/login redirect target is a real page, and so logout
works the moment sessions can be issued.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.auth.sessions import SessionManager
from jarvis.web.auth_middleware import LOGIN_PATH, auth_config

router = APIRouter()

_LOGIN_PLACEHOLDER = """<!doctype html>
<html>
  <head><title>Jarvis — sign in</title></head>
  <body>
    <h1>Jarvis</h1>
    <p>Sign-in is not available yet: the login flow ships in an upcoming
    release. If you are seeing this page unexpectedly, set
    <code>auth.enabled: false</code> in <code>config/jarvis.yaml</code>.</p>
  </body>
</html>
"""


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTMLResponse(_LOGIN_PLACEHOLDER)


@router.post("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse(LOGIN_PATH, status_code=302)
    cfg = auth_config(request)
    if cfg is None:
        return response
    manager = SessionManager(session_factory=request.app.state.ctx.session_factory, config=cfg)
    raw_token = request.cookies.get(manager.cookie_name)
    if raw_token:
        await manager.revoke(raw_token)
    manager.clear_session_cookie(response)
    return response

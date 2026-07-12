"""GET /events/stream — SSE endpoint for live audit event tailing."""

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jarvis.auth.sessions import SessionManager
from jarvis.web.auth_middleware import auth_config
from jarvis.web.time_format import configured_timezone, format_server_local

router = APIRouter()

# Re-validate the stream's session every this many loop iterations (~1s each).
# A stream opened before a revocation would otherwise keep streaming forever —
# the middleware only checks at request time, and EventSource cannot send
# custom headers, which is why the session rides the cookie here.
SESSION_RECHECK_LOOPS = 10


@router.get("/events/stream")
async def events_stream(request: Request):
    ctx = request.app.state.ctx
    timezone = configured_timezone(ctx)

    auth_cfg = auth_config(request)
    session_manager = (
        SessionManager(session_factory=ctx.session_factory, config=auth_cfg)
        if auth_cfg is not None and auth_cfg.enabled
        else None
    )

    async def _session_still_valid() -> bool:
        if session_manager is None:
            return True
        raw_token = request.cookies.get(session_manager.cookie_name)
        if not raw_token:
            return False
        return await session_manager.validate(raw_token) is not None

    async def _generate():
        last_seen = datetime.now(UTC)
        loops = 0
        yield ": connected\n\n"  # initial SSE comment so headers flush immediately
        try:
            while True:
                loops += 1
                if loops % SESSION_RECHECK_LOOPS == 0 and not await _session_still_valid():
                    return
                # Sleep in small increments so disconnect is detected promptly.
                for _ in range(10):
                    await asyncio.sleep(0.1)
                    if await request.is_disconnected():
                        return
                async with ctx.session_factory() as session:
                    # Fetch events newer than our last check.
                    from sqlalchemy import select

                    from jarvis.persistence.models import AuditEventRow

                    result = await session.execute(
                        select(AuditEventRow)
                        .where(AuditEventRow.created_at > last_seen)
                        .order_by(AuditEventRow.created_at.asc())
                        .limit(50)
                    )
                    rows = list(result.scalars())

                for row in rows:
                    data = json.dumps(
                        {
                            "type": row.type,
                            "payload": row.payload,
                            "created_at": format_server_local(
                                row.created_at,
                                "%Y-%m-%d %H:%M:%S",
                                timezone=timezone,
                            ),
                        },
                        default=str,
                    )
                    yield f"event: audit\ndata: {data}\n\n"
                    last_seen = max(last_seen, row.created_at)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(_generate(), media_type="text/event-stream")

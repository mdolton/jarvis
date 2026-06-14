"""GET /events/stream — SSE endpoint for live audit event tailing."""

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jarvis.web.time_format import format_server_local

router = APIRouter()


@router.get("/events/stream")
async def events_stream(request: Request):
    ctx = request.app.state.ctx

    async def _generate():
        last_seen = datetime.now(UTC)
        yield ": connected\n\n"  # initial SSE comment so headers flush immediately
        try:
            while True:
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
                            ),
                        },
                        default=str,
                    )
                    yield f"event: audit\ndata: {data}\n\n"
                    last_seen = max(last_seen, row.created_at)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(_generate(), media_type="text/event-stream")

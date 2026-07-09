"""POST /events/webhook — authenticated inbound event receiver.

The producer side of event-driven invocation: validates and authenticates the
payload, then hands it to the EventCoalescer, which merges bursts and enqueues
a single agent turn through the dispatcher. Responds immediately; the turn
runs in the background.
"""

import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

router = APIRouter()


class WebhookEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=100_000)
    # Trusted standing instruction — the bearer token means the sender is
    # operator-configured, same trust level as a schedule's prompt.
    prompt: str | None = Field(default=None, max_length=10_000)
    coalesce_key: str | None = Field(default=None, max_length=200)


@router.post("/events/webhook", status_code=202)
async def receive_event(request: Request):
    ctx = request.app.state.ctx
    token = ctx.config.jarvis.events.webhook_token if ctx is not None else None
    if not token:
        # Feature off: hide the endpoint entirely.
        raise HTTPException(status_code=404)

    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    # Body is parsed only after auth so unauthenticated callers learn nothing
    # about the schema.
    try:
        payload = WebhookEventIn.model_validate_json(await request.body())
    except ValidationError as exc:
        detail = exc.errors(include_url=False, include_input=False, include_context=False)
        raise HTTPException(status_code=422, detail=detail) from exc

    status = ctx.event_coalescer.submit(
        source=payload.source,
        external_id=payload.external_id,
        content=payload.content,
        prompt=payload.prompt,
        coalesce_key=payload.coalesce_key,
    )
    return {"status": status}

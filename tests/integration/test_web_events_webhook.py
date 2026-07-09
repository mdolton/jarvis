"""POST /events/webhook end-to-end: auth, dedup, coalescing, agent turn.

Uses the real FastAPI app, a real TriggerDispatcher + AgentRunner (fake model),
and a real EventCoalescer so the whole producer path is exercised on one loop.
"""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest_asyncio
from agents import set_trace_processors
from agents.models.interface import Model

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import EventsConfig, LLMConfig
from jarvis.core.coalescer import EventCoalescer
from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.types import EventTrigger, TriggerSource
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app

_WINDOW = 0.05
_TOKEN = "test-webhook-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _make_response(text: str):
    from agents.items import ModelResponse, Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    msg = ResponseOutputMessage(
        id="msg-1",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )
    return ModelResponse(output=[msg], usage=Usage(), response_id=None)


class _CountingFakeModel(Model):
    def __init__(self) -> None:
        self.calls = 0
        self.called = asyncio.Event()

    async def get_response(self, *a, **kw):
        self.calls += 1
        self.called.set()
        return _make_response(f"reply-{self.calls}")

    async def stream_response(self, *a, **kw):
        if False:
            yield None


class _RecordingDispatcher(TriggerDispatcher):
    """Records event triggers so tests can inspect the merged turn."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.event_triggers: list[EventTrigger] = []

    async def dispatch_event(self, trigger: EventTrigger):
        self.event_triggers.append(trigger)
        return await super().dispatch_event(trigger)


@pytest_asyncio.fixture(loop_scope="function")
async def harness(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    set_trace_processors([JarvisTraceProcessor(audit)])

    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = _RecordingDispatcher(runner=runner, audit=audit)
    coalescer = EventCoalescer(dispatcher=dispatcher, window_sec=_WINDOW)

    ctx = MagicMock()
    ctx.config.jarvis.events = EventsConfig(
        webhook_token=_TOKEN, coalesce_window_sec=_WINDOW
    )
    ctx.event_coalescer = coalescer

    app = create_app(app_context=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://jarvis.test") as client:
        yield client, model, dispatcher, ctx

    await coalescer.shutdown()
    await audit.stop()
    await engine.dispose()


def _event(i: int = 1, **overrides) -> dict:
    payload = {
        "source": "email",
        "external_id": f"msg-{i}",
        "content": f"New mail body {i}",
        "prompt": "Triage this mail.",
    }
    payload.update(overrides)
    return payload


async def _wait_for_first_call(model) -> None:
    try:
        async with asyncio.timeout(2.0):
            await model.called.wait()
    except TimeoutError:
        raise AssertionError("agent turn never ran") from None


async def test_webhook_post_triggers_agent_turn_within_window(harness):
    client, model, dispatcher, _ = harness

    resp = await client.post("/events/webhook", json=_event(), headers=_AUTH)

    assert resp.status_code == 202
    assert resp.json() == {"status": "queued"}
    await _wait_for_first_call(model)

    (trigger,) = dispatcher.event_triggers
    assert isinstance(trigger, EventTrigger)
    assert trigger.source == "email"
    # The turn runs as an event trigger, i.e. under the reduced tool scope
    # (enforcement itself is covered by test_event_trigger_tool_scope.py).
    from jarvis.core.types import InvocationRequest

    assert InvocationRequest(trigger=trigger).trigger_source == TriggerSource.EVENT


async def test_burst_coalesces_into_single_turn(harness):
    client, model, dispatcher, _ = harness

    for i in range(3):
        resp = await client.post("/events/webhook", json=_event(i), headers=_AUTH)
        assert resp.status_code == 202
        assert resp.json() == {"status": "queued"}

    await _wait_for_first_call(model)
    await asyncio.sleep(_WINDOW * 2)  # a second turn would have fired by now

    assert model.calls == 1
    (trigger,) = dispatcher.event_triggers
    for i in range(3):
        assert f"New mail body {i}" in trigger.content


async def test_redelivered_webhook_is_reported_duplicate_and_not_rerun(harness):
    client, model, _dispatcher, _ = harness

    first = await client.post("/events/webhook", json=_event(1), headers=_AUTH)
    dup = await client.post("/events/webhook", json=_event(1), headers=_AUTH)

    assert first.json() == {"status": "queued"}
    assert dup.json() == {"status": "duplicate"}
    await _wait_for_first_call(model)
    await asyncio.sleep(_WINDOW * 2)
    assert model.calls == 1


async def test_missing_or_wrong_token_is_401_and_never_reaches_coalescer(harness):
    client, model, _, _ = harness

    no_auth = await client.post("/events/webhook", json=_event())
    bad_auth = await client.post(
        "/events/webhook", json=_event(), headers={"Authorization": "Bearer wrong"}
    )

    assert no_auth.status_code == 401
    assert bad_auth.status_code == 401
    await asyncio.sleep(_WINDOW * 2)
    assert model.calls == 0


async def test_unconfigured_endpoint_is_404(harness):
    client, model, _, ctx = harness
    ctx.config.jarvis.events = EventsConfig()  # no token → feature off

    resp = await client.post("/events/webhook", json=_event(), headers=_AUTH)

    assert resp.status_code == 404
    assert model.calls == 0


async def test_invalid_body_is_422_only_after_auth(harness):
    client, _, _, _ = harness
    bad = {"source": "email"}  # missing external_id/content

    unauthenticated = await client.post("/events/webhook", json=bad)
    authenticated = await client.post("/events/webhook", json=bad, headers=_AUTH)

    assert unauthenticated.status_code == 401  # auth is checked before parsing
    assert authenticated.status_code == 422


async def test_cross_origin_browser_post_is_blocked(harness):
    client, model, _, _ = harness

    resp = await client.post(
        "/events/webhook",
        json=_event(),
        headers={**_AUTH, "Origin": "http://evil.example"},
    )

    assert resp.status_code == 403
    assert model.calls == 0

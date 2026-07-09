import asyncio

import pytest_asyncio
from agents import set_trace_processors
from agents.models.interface import Model

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.channels.base import OutboundMessage
from jarvis.config.schema import LLMConfig
from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.output_router import OutputRouter
from jarvis.core.types import (
    ChannelKind,
    ChannelMessage,
)
from jarvis.persistence.db import Base, create_engine, session_factory


def _make_response(text: str):
    """Build the SDK-shaped ModelResponse for a single text reply."""
    from agents.items import ModelResponse, Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    msg = ResponseOutputMessage(
        id=f"msg-{text[:8]}",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text=text,
                annotations=[],
            ),
        ],
    )
    return ModelResponse(output=[msg], usage=Usage(), response_id=None)


class _CountingFakeModel(Model):
    """Counts how many times the SDK asked for a response."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(self, *a, **kw):
        self.calls += 1
        return _make_response(f"reply-{self.calls}")

    async def stream_response(self, *a, **kw):
        if False:
            yield None


class _SlowModel(Model):
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_response(self, *a, **kw):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.05)
            return _make_response("done")
        finally:
            self.in_flight -= 1

    async def stream_response(self, *a, **kw):
        if False:
            yield None


@pytest_asyncio.fixture(loop_scope="function")
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    set_trace_processors([JarvisTraceProcessor(audit)])

    yield engine, factory, audit

    await audit.stop()
    await engine.dispose()


async def test_dispatch_manual_trigger_runs(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(runner=runner, audit=audit)

    result = await dispatcher.dispatch_manual(user="mark", prompt="hi")
    assert "reply-1" in result.final_output
    assert model.calls == 1


async def test_dispatch_dedups_discord_message_by_external_id(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(runner=runner, audit=audit)

    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        text="same question",
        external_id="discord-msg-42",
    )
    first = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})
    second = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})

    assert first is not None
    assert second is None  # dedup suppressed
    assert model.calls == 1


async def test_dispatch_rejects_disallowed_discord_user(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(runner=runner, audit=audit)

    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="stranger",
        text="hi",
        external_id="msg-1",
    )
    result = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})
    assert result is None
    assert model.calls == 0


async def test_dispatch_concurrency_is_bounded(infra):
    _, factory, audit = infra
    model = _SlowModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(
        runner=runner,
        audit=audit,
        max_concurrent=2,
    )

    # Kick off 5 concurrent manual runs; gate should cap in-flight at 2.
    tasks = [
        asyncio.create_task(dispatcher.dispatch_manual(user=f"u{i}", prompt="go")) for i in range(5)
    ]
    await asyncio.gather(*tasks)
    assert model.max_in_flight <= 2


async def test_dispatch_channel_message_routes_reply_to_adapter(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )

    sent_messages: list[OutboundMessage] = []

    class _Recorder:
        kind = ChannelKind.DISCORD.value

        async def start(self, dispatcher) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def send(self, msg: OutboundMessage) -> None:
            sent_messages.append(msg)

    router = OutputRouter(adapters=[_Recorder()])
    dispatcher = TriggerDispatcher(runner=runner, audit=audit, output_router=router)

    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        text="hello jarvis",
        external_id="msg-routing-1",
    )
    result = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})
    assert result is not None
    assert "reply-1" in result.final_output

    assert len(sent_messages) == 1
    assert sent_messages[0].channel_ref == "user-1"
    assert sent_messages[0].text == result.final_output


async def test_dispatch_event_dedups_by_external_id(infra):
    from jarvis.core.types import EventTrigger

    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )
    dispatcher = TriggerDispatcher(runner=runner, audit=audit)

    trigger = EventTrigger(
        source="calendar",
        external_id="invite-42",
        prompt="Summarize this invite.",
        content="Team sync at 3pm",
    )
    first = await dispatcher.dispatch_event(trigger)
    second = await dispatcher.dispatch_event(trigger)

    assert first is not None
    assert second is None  # dedup suppressed
    assert model.calls == 1


async def test_dispatch_manual_does_not_route(infra):
    """Manual triggers go through the CLI/dashboard path, not channel routing."""
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )

    sent_messages: list[OutboundMessage] = []

    class _Recorder:
        kind = ChannelKind.DISCORD.value

        async def start(self, dispatcher) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def send(self, msg: OutboundMessage) -> None:
            sent_messages.append(msg)

    router = OutputRouter(adapters=[_Recorder()])
    dispatcher = TriggerDispatcher(runner=runner, audit=audit, output_router=router)

    await dispatcher.dispatch_manual(user="mark", prompt="hi")

    assert sent_messages == []

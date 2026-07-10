"""AgentRunner streamed path: SDK stream events are pumped into the RunStream.

Runner.run_streamed is monkeypatched with a fake result whose stream_events()
yields synthetic SDK events — we assert on what reaches the stream and on the
persisted messages, not on SDK internals.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from agents import set_trace_processors
from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.types import ChannelKind, ChannelMessage, InvocationRequest, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MessageRepo


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


class _RecordingStream:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.statuses: list[str | None] = []
        self.finished: list[str] = []
        self.closed = False
        self.delivered = False

    async def update(self, text: str) -> None:
        self.updates.append(text)

    async def status(self, label: str | None) -> None:
        self.statuses.append(label)

    async def finish(self, final_text: str) -> None:
        self.finished.append(final_text)
        self.delivered = True

    async def close(self) -> None:
        self.closed = True


def _delta(text: str) -> RawResponsesStreamEvent:
    return RawResponsesStreamEvent(
        data=SimpleNamespace(type="response.output_text.delta", delta=text)
    )


def _tool_called(name: str) -> RunItemStreamEvent:
    return RunItemStreamEvent(
        name="tool_called", item=SimpleNamespace(raw_item=SimpleNamespace(name=name))
    )


def _tool_output() -> RunItemStreamEvent:
    return RunItemStreamEvent(name="tool_output", item=SimpleNamespace(raw_item=None))


class _FakeStreamedResult:
    def __init__(self, events, final_output: str) -> None:
        self._events = list(events)
        self.final_output = final_output
        self.interruptions: list = []
        self.is_complete = False
        self.cancelled = False

    async def stream_events(self):
        for e in self._events:
            yield e
        self.is_complete = True

    def cancel(self) -> None:
        self.cancelled = True


def _runner(factory, audit) -> AgentRunner:
    return AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model="fake-model",  # never resolved; Runner.run/run_streamed are patched
    )


def _request() -> InvocationRequest:
    return InvocationRequest(
        trigger=ChannelMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="42",
            text="hi",
            external_id="m1",
        )
    )


async def test_streamed_run_pumps_deltas_and_tool_status(infra, monkeypatch):
    _, factory, audit = infra
    fake = _FakeStreamedResult(
        [
            _delta("Let me check."),
            _tool_called("web_search"),
            _tool_output(),
            _delta(" Found it."),
        ],
        final_output="Here is the answer.",
    )
    monkeypatch.setattr(
        "jarvis.agents.runner.Runner.run_streamed",
        lambda agent, run_input, run_config=None: fake,
    )

    stream = _RecordingStream()
    result = await _runner(factory, audit).run(_request(), stream=stream)

    assert stream.updates == ["Let me check.", "Let me check. Found it."]
    assert stream.statuses == ["web_search", None]
    assert result.final_output == "Here is the answer."
    # Delivery (finish) belongs to the dispatcher, not the runner.
    assert stream.finished == []

    async with factory() as s:
        from sqlalchemy import select

        from jarvis.persistence.models import ConversationRow

        conv = (await s.execute(select(ConversationRow))).scalars().one()
        msgs = await MessageRepo(s).history(conv.id)
    assert [m.role for m in msgs] == [MessageRole.USER.value, MessageRole.ASSISTANT.value]
    assert msgs[1].content == "Here is the answer."


async def test_no_stream_keeps_plain_run_path(infra, monkeypatch):
    _, factory, audit = infra
    called = {"run": 0, "run_streamed": 0}

    async def fake_run(agent, run_input, run_config=None):
        called["run"] += 1
        return SimpleNamespace(final_output="plain")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)
    monkeypatch.setattr(
        "jarvis.agents.runner.Runner.run_streamed",
        lambda *a, **kw: called.__setitem__("run_streamed", called["run_streamed"] + 1),
    )

    result = await _runner(factory, audit).run(_request())
    assert result.final_output == "plain"
    assert called == {"run": 1, "run_streamed": 0}


async def test_streamed_run_cancels_sdk_result_when_iteration_dies(infra, monkeypatch):
    _, factory, audit = infra

    class _ExplodingResult(_FakeStreamedResult):
        async def stream_events(self):
            yield _delta("partial")
            raise RuntimeError("model exploded")

    fake = _ExplodingResult([], final_output=None)
    monkeypatch.setattr(
        "jarvis.agents.runner.Runner.run_streamed",
        lambda agent, run_input, run_config=None: fake,
    )

    stream = _RecordingStream()
    with pytest.raises(RuntimeError, match="model exploded"):
        await _runner(factory, audit).run(_request(), stream=stream)
    assert fake.cancelled is True

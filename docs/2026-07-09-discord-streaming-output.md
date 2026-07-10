# Discord Streaming Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single final Discord message with a live-updating draft message (streamed text + tool status) and a typing indicator, so multi-step runs are visibly progressing instead of going silent then dumping a block.

**Architecture:** The Discord adapter gains `open_stream(channel_ref)` returning a `DiscordMessageStream` (draft-message editor with throttled edits + typing context manager). The dispatcher asks the `OutputRouter` to open a stream for channel-message triggers, passes it to `AgentRunner.run(request, stream=...)`; the runner switches from `Runner.run` to `Runner.run_streamed` when a stream is present and pumps SDK stream events into the stream (`update` for text deltas, `status` for tool calls). The dispatcher finalizes delivery (`stream.finish(final_output)`) and falls back to the existing `OutputRouter.route` send when the stream didn't deliver. Non-Discord and non-channel triggers are untouched (stream=None → existing `Runner.run` path).

**Tech Stack:** discord.py 2.7 (`Messageable.typing()` async CM, `Message.edit`), openai-agents 0.14 (`Runner.run_streamed`, `RawResponsesStreamEvent` with `data.type == "response.output_text.delta"`, `RunItemStreamEvent` names `tool_called`/`tool_output`/`message_output_created`; `RunResultStreaming` has `interruptions`/`to_state` like `RunResult`).

## Global Constraints

- Python 3.12, ruff line-length 100, pytest `asyncio_mode = auto` (no `@pytest.mark.asyncio`).
- Discord message limit: 2000 chars. Edit throttle: min 1.5s between draft edits (Discord edit bucket ≈ 5 req/5s).
- Typing indicator must ALWAYS clear: `close()` is idempotent and called in a dispatcher `finally`.
- Stream failures must NEVER fail the run: every Discord error inside the stream is caught/logged; the stream degrades to inert and `delivered` stays False so the router's plain send still fires.
- No changes to scheduled/event/manual paths; monkeypatched `Runner.run` in existing tests must keep working (stream=None path calls `Runner.run` with identical arguments).
- `make check` green before PR. Branch off main; never push to main. Co-author trailer on commits.

---

### Task 1: `RunStream` protocol + `DiscordMessageStream`

**Files:**
- Create: `jarvis/core/streaming.py`
- Create: `jarvis/channels/discord_stream.py`
- Test: `tests/integration/test_discord_stream.py`

**Interfaces:**
- Produces: `jarvis.core.streaming.RunStream` protocol: attribute `delivered: bool`; methods `async update(text: str) -> None`, `async status(label: str | None) -> None`, `async finish(final_text: str) -> None`, `async close() -> None`.
- Produces: `jarvis.channels.discord_stream.DiscordMessageStream(channel=..., min_edit_interval: float = 1.5, clock: Callable[[], float] | None = None)` with `async start()` plus the RunStream surface.

- [ ] **Step 1: Write `jarvis/core/streaming.py`** (pure protocol, no test needed beyond import in Task 1 tests)

```python
"""RunStream — channel-agnostic contract for streaming agent output.

A RunStream is opened per run by the OutputRouter (adapter-specific), fed by
the AgentRunner while the run progresses, and finalized by the dispatcher.
Implementations must be failure-proof: no method may raise into the run —
streaming is best-effort decoration on top of the plain final send.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class RunStream(Protocol):
    """Live output surface for one agent run."""

    delivered: bool
    """True only after finish() successfully placed the final text; the
    caller falls back to a plain adapter send when False."""

    async def update(self, text: str) -> None:
        """Replace the in-progress text (full accumulated text, not a delta)."""
        ...

    async def status(self, label: str | None) -> None:
        """Show (or clear, with None) a transient activity label, e.g. a tool name."""
        ...

    async def finish(self, final_text: str) -> None:
        """Deliver the final text and stop all in-progress affordances."""
        ...

    async def close(self) -> None:
        """Idempotent cleanup; must always stop the typing indicator."""
        ...
```

- [ ] **Step 2: Write failing tests for `DiscordMessageStream`**

`tests/integration/test_discord_stream.py`:

```python
"""DiscordMessageStream unit tests with a fake channel and injected clock."""

from unittest.mock import AsyncMock, MagicMock

from jarvis.channels.discord_stream import DiscordMessageStream
from jarvis.core.streaming import RunStream


class _FakeTyping:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        self.exited += 1
        return False


def _channel(typing: _FakeTyping | None = None):
    ch = MagicMock()
    ch.typing = MagicMock(return_value=typing or _FakeTyping())
    draft = MagicMock()
    draft.edit = AsyncMock()
    ch.send = AsyncMock(return_value=draft)
    return ch, draft


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_satisfies_run_stream_protocol():
    ch, _ = _channel()
    assert isinstance(DiscordMessageStream(channel=ch), RunStream)


async def test_start_enters_typing_and_close_exits_it():
    typing = _FakeTyping()
    ch, _ = _channel(typing)
    stream = DiscordMessageStream(channel=ch)
    await stream.start()
    assert typing.entered == 1
    await stream.close()
    assert typing.exited == 1
    await stream.close()  # idempotent
    assert typing.exited == 1


async def test_first_update_creates_draft_then_edits_are_throttled():
    ch, draft = _channel()
    clock = _Clock()
    stream = DiscordMessageStream(channel=ch, min_edit_interval=1.5, clock=clock)
    await stream.start()

    await stream.update("Hel")
    ch.send.assert_awaited_once_with("Hel")

    clock.now += 0.2
    await stream.update("Hello wor")  # too soon: dropped
    draft.edit.assert_not_awaited()

    clock.now += 1.4  # 1.6s since draft creation
    await stream.update("Hello world")
    draft.edit.assert_awaited_once_with(content="Hello world")


async def test_status_renders_activity_line():
    ch, draft = _channel()
    clock = _Clock()
    stream = DiscordMessageStream(channel=ch, clock=clock)
    await stream.start()

    await stream.status("web_search")
    ch.send.assert_awaited_once_with("⚙️ *web_search…*")

    clock.now += 2.0
    await stream.update("Found three results.")
    draft.edit.assert_awaited_once_with(content="Found three results.\n\n⚙️ *web_search…*")

    clock.now += 2.0
    await stream.status(None)
    assert draft.edit.await_args.kwargs == {"content": "Found three results."}


async def test_finish_edits_draft_bypassing_throttle_and_clears_typing():
    typing = _FakeTyping()
    ch, draft = _channel(typing)
    clock = _Clock()
    stream = DiscordMessageStream(channel=ch, clock=clock)
    await stream.start()
    await stream.update("partial")

    await stream.finish("the full final answer")  # 0s after last edit
    draft.edit.assert_awaited_once_with(content="the full final answer")
    assert stream.delivered is True
    assert typing.exited == 1


async def test_finish_without_draft_sends_message():
    ch, _ = _channel()
    stream = DiscordMessageStream(channel=ch)
    await stream.start()
    await stream.finish("short answer")
    ch.send.assert_awaited_once_with("short answer")
    assert stream.delivered is True


async def test_finish_chunks_over_discord_limit():
    ch, draft = _channel()
    stream = DiscordMessageStream(channel=ch)
    await stream.start()
    await stream.update("partial")
    ch.send.reset_mock()

    long_text = "x" * 4500
    await stream.finish(long_text)
    draft.edit.assert_awaited_once_with(content="x" * 2000)
    assert [c.args[0] for c in ch.send.await_args_list] == ["x" * 2000, "x" * 500]
    assert stream.delivered is True


async def test_streaming_preview_is_capped():
    ch, _ = _channel()
    stream = DiscordMessageStream(channel=ch)
    await stream.start()
    await stream.update("y" * 3000)
    sent = ch.send.await_args.args[0]
    assert len(sent) <= 2000
    assert sent.endswith("…")


async def test_discord_errors_never_raise_and_stream_goes_inert():
    typing = _FakeTyping()
    ch, _ = _channel(typing)
    ch.send = AsyncMock(side_effect=RuntimeError("discord down"))
    clock = _Clock()
    stream = DiscordMessageStream(channel=ch, clock=clock)
    await stream.start()

    for _ in range(5):  # never raises, gives up after max failures
        clock.now += 2.0
        await stream.update("text")
    assert ch.send.await_count == 3  # _MAX_FAILURES

    await stream.finish("final")
    assert stream.delivered is False  # caller must fall back to plain send
    assert typing.exited == 1  # typing still cleared


async def test_close_without_finish_leaves_delivered_false():
    ch, _ = _channel()
    stream = DiscordMessageStream(channel=ch)
    await stream.start()
    await stream.update("partial")
    await stream.close()
    assert stream.delivered is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_discord_stream.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jarvis.channels.discord_stream'`

- [ ] **Step 4: Write `jarvis/channels/discord_stream.py`**

```python
"""DiscordMessageStream — streams agent output by live-editing a draft DM.

Lifecycle: start() enters the typing context (discord.py re-triggers it every
few seconds, so the indicator persists through silent tool phases). update()/
status() render into one draft message, throttled to at most one edit per
`min_edit_interval` seconds (Discord's per-channel edit bucket is ~5 req/5s —
dropped intermediate frames are fine because the next render or finish()
carries the full text). finish() bypasses the throttle, chunks text over the
2000-char limit, and sets `delivered`. close() is the idempotent safety net:
it always exits the typing context.

Failure policy: streaming is best-effort decoration. Every Discord error is
caught and logged; after _MAX_FAILURES consecutive render failures the stream
goes inert. `delivered` is True only when finish() actually placed the final
text, so the caller can fall back to a plain send.
"""

import asyncio
import logging
from collections.abc import Callable

_log = logging.getLogger(__name__)

_MESSAGE_LIMIT = 2000
_PREVIEW_LIMIT = 1900  # streaming cap; leaves room for the status line


def _chunks(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    text = text.strip()
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class DiscordMessageStream:
    _MAX_FAILURES = 3

    def __init__(
        self,
        *,
        channel,
        min_edit_interval: float = 1.5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._channel = channel
        self._interval = min_edit_interval
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._typing = None
        self._draft = None
        self._text = ""
        self._status: str | None = None
        self._last_edit = float("-inf")
        self._failures = 0
        self._closed = False
        self.delivered = False

    async def start(self) -> None:
        try:
            typing = self._channel.typing()
            await typing.__aenter__()
            self._typing = typing
        except Exception:
            _log.exception("discord stream: could not start typing indicator")

    async def update(self, text: str) -> None:
        self._text = text
        await self._render()

    async def status(self, label: str | None) -> None:
        self._status = label
        await self._render()

    async def finish(self, final_text: str) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_typing()
        chunks = _chunks(final_text)
        if not chunks:
            return
        try:
            if self._draft is None:
                await self._channel.send(chunks[0])
            else:
                await self._draft.edit(content=chunks[0])
            for extra in chunks[1:]:
                await self._channel.send(extra)
        except Exception:
            _log.exception("discord stream: final edit failed; caller will fall back")
            return
        self.delivered = True

    async def close(self) -> None:
        self._closed = True
        await self._stop_typing()

    async def _stop_typing(self) -> None:
        typing, self._typing = self._typing, None
        if typing is None:
            return
        try:
            await typing.__aexit__(None, None, None)
        except Exception:
            _log.exception("discord stream: failed to clear typing indicator")

    async def _render(self) -> None:
        if self._closed or self._failures >= self._MAX_FAILURES:
            return
        content = self._compose()
        if not content:
            return
        now = self._clock()
        if self._draft is not None and (now - self._last_edit) < self._interval:
            return  # dropped frame; the next render or finish() carries the text
        try:
            if self._draft is None:
                self._draft = await self._channel.send(content)
            else:
                await self._draft.edit(content=content)
        except Exception:
            self._failures += 1
            _log.warning(
                "discord stream: draft render failed (%d/%d)",
                self._failures,
                self._MAX_FAILURES,
                exc_info=True,
            )
            return
        self._last_edit = now
        self._failures = 0

    def _compose(self) -> str:
        text = self._text.strip()
        if len(text) > _PREVIEW_LIMIT:
            text = text[:_PREVIEW_LIMIT] + " …"
        if self._status:
            line = f"⚙️ *{self._status}…*"
            return f"{text}\n\n{line}" if text else line
        return text
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_discord_stream.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add jarvis/core/streaming.py jarvis/channels/discord_stream.py tests/integration/test_discord_stream.py
git commit -m "feat: DiscordMessageStream — throttled draft-edit streaming with typing indicator"
```

---

### Task 2: `DiscordAdapter.open_stream`

**Files:**
- Modify: `jarvis/channels/discord_adapter.py` (add method after `send`, ~line 183)
- Test: `tests/integration/test_discord_adapter_send.py` (append)

**Interfaces:**
- Consumes: `DiscordMessageStream` from Task 1.
- Produces: `DiscordAdapter.open_stream(channel_ref: str) -> DiscordMessageStream | None` — None (never raise) when the client isn't ready, the ref is bad, or Discord errors.

- [ ] **Step 1: Write failing tests** (append to `tests/integration/test_discord_adapter_send.py`)

```python
class _FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_open_stream_returns_started_stream():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})

    channel = MagicMock()
    channel.typing = MagicMock(return_value=_FakeTyping())
    fake_user = MagicMock()
    fake_user.dm_channel = None
    fake_user.create_dm = AsyncMock(return_value=channel)
    fake_client = MagicMock()
    fake_client.fetch_user = AsyncMock(return_value=fake_user)
    adapter._client = fake_client
    adapter._ready.set()

    stream = await adapter.open_stream("111")
    assert stream is not None
    fake_client.fetch_user.assert_awaited_once_with(111)
    channel.typing.assert_called_once()  # start() entered typing
    await stream.close()


async def test_open_stream_returns_none_when_not_ready():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    assert await adapter.open_stream("111") is None


async def test_open_stream_returns_none_on_bad_ref_or_error():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    fake_client = MagicMock()
    fake_client.fetch_user = AsyncMock(side_effect=RuntimeError("boom"))
    adapter._client = fake_client
    adapter._ready.set()

    assert await adapter.open_stream("not-a-number") is None
    assert await adapter.open_stream("111") is None  # fetch_user error swallowed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_discord_adapter_send.py -q`
Expected: FAIL — `AttributeError: 'DiscordAdapter' object has no attribute 'open_stream'`

- [ ] **Step 3: Implement `open_stream`** in `jarvis/channels/discord_adapter.py` after `send()`; add import `from jarvis.channels.discord_stream import DiscordMessageStream`

```python
    async def open_stream(self, channel_ref: str) -> DiscordMessageStream | None:
        """Open a live-editing draft stream to a DM. Best-effort: any failure
        returns None and the caller falls back to a plain send()."""
        if self._client is None or not self._ready.is_set():
            return None
        try:
            user_id = int(channel_ref)
        except ValueError:
            return None
        try:
            user = await self._client.fetch_user(user_id)
            channel = user.dm_channel or await user.create_dm()
        except Exception:
            _log.exception("failed to open discord stream; falling back to plain send")
            return None
        stream = DiscordMessageStream(channel=channel)
        await stream.start()
        return stream
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_discord_adapter_send.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/channels/discord_adapter.py tests/integration/test_discord_adapter_send.py
git commit -m "feat: DiscordAdapter.open_stream — best-effort DM stream opener"
```

---

### Task 3: Runner streamed execution path

**Files:**
- Modify: `jarvis/agents/runner.py` (`run()` signature, the `Runner.run` call site under `trigger_scope`, new helpers `_execute`, `_pump_stream_events`, `_tool_label`)
- Test: `tests/integration/test_agent_runner_streaming.py`

**Interfaces:**
- Consumes: `RunStream` protocol (Task 1).
- Produces: `AgentRunner.run(request, stream: RunStream | None = None)`. With stream=None behavior is byte-identical to today (`Runner.run`). With a stream: uses `Runner.run_streamed`, forwards `response.output_text.delta` accumulations to `stream.update`, `tool_called`→`stream.status(name)`, `tool_output`→`stream.status(None)`. The runner does NOT call finish() — delivery is the dispatcher's job (Task 4).

- [ ] **Step 1: Write failing tests**

`tests/integration/test_agent_runner_streaming.py`:

```python
"""AgentRunner streamed path: SDK stream events are pumped into the RunStream.

Runner.run_streamed is monkeypatched with a fake result whose stream_events()
yields synthetic SDK events — we assert on what reaches the stream and on the
persisted messages, not on SDK internals.
"""

from types import SimpleNamespace

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
        model=SimpleNamespace(),  # never reached; run_streamed is patched
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
    import pytest

    with pytest.raises(RuntimeError, match="model exploded"):
        await _runner(factory, audit).run(_request(), stream=stream)
    assert fake.cancelled is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_agent_runner_streaming.py -q`
Expected: FAIL — `TypeError: AgentRunner.run() got an unexpected keyword argument 'stream'`

- [ ] **Step 3: Implement in `jarvis/agents/runner.py`**

Add import: `from jarvis.core.streaming import RunStream`.

Change signature: `async def run(self, request: InvocationRequest, stream: RunStream | None = None) -> AgentRunResult:`

Replace the `trigger_scope` block (currently the two `Runner.run` calls):

```python
        with trigger_scope(request.trigger_source):
            if self._run_timeout_sec is None:
                sdk_result = await self._execute(agent, run_input, stream)
            else:
                async with asyncio.timeout(self._run_timeout_sec):
                    sdk_result = await self._execute(agent, run_input, stream)
```

Add methods/helpers:

```python
    async def _execute(self, agent, run_input, stream: RunStream | None):
        """One turn against the SDK: plain run, or streamed when a RunStream
        is attached (channel messages from adapters that support streaming)."""
        run_config = RunConfig(workflow_name="jarvis-invoke")
        if stream is None:
            return await Runner.run(agent, run_input, run_config=run_config)
        sdk_result = Runner.run_streamed(agent, run_input, run_config=run_config)
        await _pump_stream_events(sdk_result, stream)
        return sdk_result
```

Module-level helpers (near `_extract_text`):

```python
async def _pump_stream_events(sdk_result, stream: RunStream) -> None:
    """Forward SDK stream events to the channel stream.

    The stream implementation swallows its own delivery errors, so only SDK
    exceptions propagate — exactly like the non-streamed path. If iteration
    dies (error, timeout cancellation), the background run loop is cancelled
    so it can't outlive the request.
    """
    acc = ""
    try:
        async for event in sdk_result.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if getattr(data, "type", None) == "response.output_text.delta":
                    acc += data.delta
                    await stream.update(acc)
            elif event.type == "run_item_stream_event":
                if event.name == "tool_called":
                    await stream.status(_tool_label(event.item))
                elif event.name == "tool_output":
                    await stream.status(None)
                elif event.name == "message_output_created" and acc and not acc.endswith("\n\n"):
                    # Separate this completed interim message from the next one.
                    acc += "\n\n"
    finally:
        if not sdk_result.is_complete:
            sdk_result.cancel()


def _tool_label(item) -> str:
    raw = getattr(item, "raw_item", None)
    return getattr(raw, "name", None) or "tool"
```

Note: `RunResultStreaming` carries `interruptions` and `to_state` just like `RunResult`, so the existing approval-interruption block after the `trigger_scope` block works unchanged for both.

- [ ] **Step 4: Run tests to verify they pass (plus the existing runner suites)**

Run: `uv run pytest tests/integration/test_agent_runner_streaming.py tests/integration/test_agent_runner.py tests/integration/test_agent_runner_actions.py tests/integration/test_agent_runner_memory.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/agents/runner.py tests/integration/test_agent_runner_streaming.py
git commit -m "feat: AgentRunner streams SDK events into an attached RunStream"
```

---

### Task 4: Dispatcher + OutputRouter wiring

**Files:**
- Modify: `jarvis/core/output_router.py` (add `OutputRouter.open_stream`)
- Modify: `jarvis/core/dispatcher.py` (`_run`)
- Test: `tests/integration/test_dispatcher_streaming.py`

**Interfaces:**
- Consumes: `AgentRunner.run(request, stream=...)` (Task 3), adapter `open_stream` (Task 2, discovered via `getattr` so the ChannelAdapter protocol is unchanged).
- Produces: `OutputRouter.open_stream(request: InvocationRequest) -> RunStream | None`; dispatcher `_run` opens the stream, finishes it with `result.final_output`, always closes it, and skips `route()` when `stream.delivered`.

- [ ] **Step 1: Write failing tests**

`tests/integration/test_dispatcher_streaming.py`:

```python
"""Dispatcher/OutputRouter streaming wiring, with the runner mocked out."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.output_router import OutputRouter
from jarvis.core.types import ChannelKind, ChannelMessage, InvocationRequest, ManualTrigger


class _Stream:
    def __init__(self, deliver: bool = True) -> None:
        self._deliver = deliver
        self.finished: list[str] = []
        self.closed = False
        self.delivered = False

    async def update(self, text: str) -> None: ...

    async def status(self, label: str | None) -> None: ...

    async def finish(self, final_text: str) -> None:
        self.finished.append(final_text)
        self.delivered = self._deliver

    async def close(self) -> None:
        self.closed = True


class _StreamingAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self, stream: _Stream | None) -> None:
        self.stream = stream
        self.sent: list = []

    async def start(self, dispatcher) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def open_stream(self, channel_ref: str):
        return self.stream


def _result(text: str = "final answer"):
    return SimpleNamespace(
        final_output=text,
        conversation_id=None,
        trigger_id=None,
        channel_kind=ChannelKind.DISCORD,
        channel_ref="42",
    )


def _msg() -> ChannelMessage:
    return ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="42",
        text="hi",
        external_id="m-1",
    )


def _dispatcher(adapter, runner):
    router = OutputRouter(adapters=[adapter])
    return TriggerDispatcher(runner=runner, audit=AsyncMock(), output_router=router)


async def test_router_open_stream_only_for_channel_messages():
    stream = _Stream()
    router = OutputRouter(adapters=[_StreamingAdapter(stream)])

    assert await router.open_stream(InvocationRequest(trigger=_msg())) is stream
    manual = InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hi"))
    assert await router.open_stream(manual) is None


async def test_router_open_stream_none_for_adapter_without_support():
    class _Plain:
        kind = ChannelKind.DISCORD.value

        async def start(self, dispatcher) -> None: ...

        async def stop(self) -> None: ...

        async def send(self, msg) -> None: ...

    router = OutputRouter(adapters=[_Plain()])
    assert await router.open_stream(InvocationRequest(trigger=_msg())) is None


async def test_delivered_stream_skips_route_send():
    stream = _Stream(deliver=True)
    adapter = _StreamingAdapter(stream)
    runner = AsyncMock()
    runner.run = AsyncMock(return_value=_result())
    dispatcher = _dispatcher(adapter, runner)

    await dispatcher.dispatch_channel_message(_msg(), allowed_refs={"42"})

    assert runner.run.await_args.kwargs["stream"] is stream
    assert stream.finished == ["final answer"]
    assert stream.closed is True
    assert adapter.sent == []  # no duplicate plain send


async def test_undelivered_stream_falls_back_to_plain_send():
    stream = _Stream(deliver=False)
    adapter = _StreamingAdapter(stream)
    runner = AsyncMock()
    runner.run = AsyncMock(return_value=_result())
    dispatcher = _dispatcher(adapter, runner)

    await dispatcher.dispatch_channel_message(_msg(), allowed_refs={"42"})

    assert stream.closed is True
    assert [m.text for m in adapter.sent] == ["final answer"]


async def test_stream_closed_even_when_runner_raises():
    stream = _Stream()
    adapter = _StreamingAdapter(stream)
    runner = AsyncMock()
    runner.run = AsyncMock(side_effect=RuntimeError("run died"))
    dispatcher = _dispatcher(adapter, runner)

    with pytest.raises(RuntimeError, match="run died"):
        await dispatcher.dispatch_channel_message(_msg(), allowed_refs={"42"})
    assert stream.closed is True
    assert stream.finished == []


async def test_open_stream_failure_does_not_block_run():
    class _Exploding(_StreamingAdapter):
        async def open_stream(self, channel_ref: str):
            raise RuntimeError("discord down")

    adapter = _Exploding(None)
    runner = AsyncMock()
    runner.run = AsyncMock(return_value=_result())
    dispatcher = _dispatcher(adapter, runner)

    await dispatcher.dispatch_channel_message(_msg(), allowed_refs={"42"})

    assert runner.run.await_args.kwargs["stream"] is None
    assert [m.text for m in adapter.sent] == ["final answer"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_dispatcher_streaming.py -q`
Expected: FAIL — `AttributeError: 'OutputRouter' object has no attribute 'open_stream'`

- [ ] **Step 3: Implement**

`jarvis/core/output_router.py` — add imports `from jarvis.core.types import ..., ChannelMessage, InvocationRequest` and `from jarvis.core.streaming import RunStream`; add method to `OutputRouter`:

```python
    async def open_stream(self, request: InvocationRequest) -> RunStream | None:
        """Open a live output stream for a channel-message run, if the
        originating adapter supports it. Never raises — a stream is an
        enhancement; the plain route() send is the guaranteed path."""
        trigger = request.trigger
        if not isinstance(trigger, ChannelMessage):
            return None
        adapter = self._by_kind.get(trigger.channel_kind.value)
        opener = getattr(adapter, "open_stream", None)
        if opener is None:
            return None
        try:
            return await opener(trigger.channel_ref)
        except Exception:
            _log.exception("open_stream failed; run continues without streaming")
            return None
```

`jarvis/core/dispatcher.py` — replace `_run`:

```python
    async def _run(self, request: InvocationRequest) -> AgentRunResult:
        stream = None
        if self._output_router is not None:
            stream = await self._output_router.open_stream(request)
        try:
            async with self._sem:
                result = await self._runner.run(request, stream=stream)
            if stream is not None:
                await stream.finish(result.final_output)
        finally:
            if stream is not None:
                await stream.close()  # idempotent; guarantees typing clears
        if self._output_router is not None and not (stream is not None and stream.delivered):
            await self._output_router.route(result)
        return result
```

- [ ] **Step 4: Run new + existing dispatcher/router tests**

Run: `uv run pytest tests/integration/test_dispatcher_streaming.py tests/integration/test_dispatcher.py tests/unit -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add jarvis/core/output_router.py jarvis/core/dispatcher.py tests/integration/test_dispatcher_streaming.py
git commit -m "feat: dispatcher opens a per-run Discord stream and finalizes delivery"
```

---

### Task 5: Full check + PR

- [ ] **Step 1:** Run `make check` (ruff + full pytest). Expected: green. Fix anything that isn't (format only files this branch touched — never blind `make fmt`).
- [ ] **Step 2:** Push branch and open PR with `gh pr create` describing the streaming design, failure modes handled (throttled edits ≥1.5s apart, typing cleared via idempotent close in a `finally`, `delivered` fallback to plain send), and test coverage. Body ends with the 🤖 generated-with trailer.

## Self-Review Notes

- Spec coverage: streaming draft edits (Task 1/3), typing indicator + guaranteed clear (Task 1 close/finish + Task 4 finally), edit rate-limit debounce (Task 1 throttle; discord.py additionally sleeps on 429 buckets), multi-step visibility (tool status lines, Task 3), `make check` (Task 5). Voice/STT deliberately excluded per goal.
- Approval-interruption path: `RunResultStreaming` exposes `interruptions`/`to_state`, so the existing block in `runner.run` is shared; the dispatcher `finish()`es with the approval notice text — verified by existing `test_agent_runner_actions.py` staying green (Task 3 Step 4).
- Existing tests that monkeypatch `Runner.run(agent, prompt, run_config=None)` keep passing: stream=None path calls `Runner.run` with the same positional/keyword shape.
- Scheduler/ActionService construct their own runners and call `run(request)` — stream defaults to None, untouched.

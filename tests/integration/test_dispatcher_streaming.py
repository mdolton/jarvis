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

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

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.channels.base import OutboundMessage
from jarvis.channels.discord_adapter import DiscordAdapter
from jarvis.core.types import ChannelKind


async def test_send_fetches_user_and_calls_send():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})

    fake_user = MagicMock()
    fake_user.send = AsyncMock()
    fake_client = MagicMock()
    fake_client.fetch_user = AsyncMock(return_value=fake_user)
    adapter._client = fake_client

    await adapter.send(
        OutboundMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="111",
            text="hello back",
        )
    )

    fake_client.fetch_user.assert_awaited_once_with(111)
    fake_user.send.assert_awaited_once_with("hello back")


def _adapter_with_user():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    fake_user = MagicMock()
    fake_user.send = AsyncMock()
    fake_client = MagicMock()
    fake_client.fetch_user = AsyncMock(return_value=fake_user)
    adapter._client = fake_client
    return adapter, fake_user


async def _send(adapter, text: str):
    await adapter.send(
        OutboundMessage(channel_kind=ChannelKind.DISCORD, channel_ref="111", text=text)
    )


async def test_send_splits_text_over_the_discord_limit():
    """A daily brief over 2000 chars used to be rejected wholesale (HTTP 400,
    error code 50035) and the run recorded as failed. Scheduled runs never
    stream, so this path — not DiscordMessageStream — carries the brief."""
    adapter, user = _adapter_with_user()
    brief = "\n".join(f"- item {i} " + "z" * 120 for i in range(50))

    await _send(adapter, brief)

    sent = [c.args[0] for c in user.send.await_args_list]
    assert len(sent) > 1
    assert all(len(chunk) <= 2000 for chunk in sent)
    assert "\n".join(sent) == brief  # nothing dropped across the seam


async def test_send_under_the_limit_stays_a_single_message():
    adapter, user = _adapter_with_user()
    await _send(adapter, "short brief")
    user.send.assert_awaited_once_with("short brief")


async def test_send_of_empty_text_still_reaches_discord():
    """An empty result must surface as a failed run, not vanish silently."""
    adapter, user = _adapter_with_user()
    await _send(adapter, "   ")
    user.send.assert_awaited_once_with("   ")


async def test_send_before_start_raises():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})

    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="111",
                text="x",
            )
        )


async def test_send_rejects_non_integer_channel_ref():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._client = MagicMock()

    with pytest.raises(ValueError, match="not a Discord user id"):
        await adapter.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="not-a-number",
                text="x",
            )
        )


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

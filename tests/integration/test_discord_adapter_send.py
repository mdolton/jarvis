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

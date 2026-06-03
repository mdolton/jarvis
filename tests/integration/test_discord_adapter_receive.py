"""DiscordAdapter receive-path tests using a stubbed discord.Client."""

from unittest.mock import MagicMock

from jarvis.channels.discord_adapter import DiscordAdapter
from jarvis.core.types import ChannelKind


class _StubDispatcher:
    """Captures dispatch_channel_message calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def dispatch_channel_message(self, msg, *, allowed_refs):
        self.calls.append((msg, allowed_refs))
        return None


def _make_dm_message(*, content: str, author_id: int, message_id: int) -> MagicMock:
    """Build a discord.Message stand-in with the fields the adapter reads."""
    msg = MagicMock()
    msg.content = content
    msg.id = message_id
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.bot = False
    import discord

    msg.channel = MagicMock(spec=discord.DMChannel)
    return msg


async def test_dm_from_allowed_user_dispatches():
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = dispatcher

    msg = _make_dm_message(content="hello", author_id=111, message_id=42)
    await adapter._on_message(msg)

    assert len(dispatcher.calls) == 1
    channel_msg, allowed_refs = dispatcher.calls[0]
    assert channel_msg.channel_kind == ChannelKind.DISCORD
    assert channel_msg.channel_ref == "111"
    assert channel_msg.text == "hello"
    assert channel_msg.external_id == "42"
    assert allowed_refs == {"111"}


async def test_dm_from_disallowed_user_is_filtered_at_adapter():
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = dispatcher

    msg = _make_dm_message(content="hi", author_id=999, message_id=1)
    await adapter._on_message(msg)

    assert dispatcher.calls == []


async def test_message_from_self_is_ignored():
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = dispatcher

    msg = _make_dm_message(content="hi", author_id=111, message_id=1)
    msg.author.bot = True

    await adapter._on_message(msg)

    assert dispatcher.calls == []


async def test_non_dm_messages_are_ignored():
    """Server-channel messages (TextChannel) should not trigger Jarvis."""
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = dispatcher

    import discord

    msg = MagicMock()
    msg.content = "hello in a server"
    msg.id = 99
    msg.author = MagicMock()
    msg.author.id = 111
    msg.author.bot = False
    msg.channel = MagicMock(spec=discord.TextChannel)

    await adapter._on_message(msg)

    assert dispatcher.calls == []


async def test_dispatch_failure_dms_user():
    class _BoomDispatcher:
        async def dispatch_channel_message(self, msg, *, allowed_refs):
            raise RuntimeError("model unavailable")

    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = _BoomDispatcher()

    msg = _make_dm_message(content="hello", author_id=111, message_id=7)
    sent = []

    async def _send(text):
        sent.append(text)

    msg.channel.send = _send

    await adapter._on_message(msg)

    assert len(sent) == 1
    assert "/model set" in sent[0]

"""DiscordAdapter — implements ChannelAdapter via discord.py.

Lifecycle:
  - start(dispatcher): set up intents, instantiate discord.Client, register
    on_message handler, schedule client.start(token) on the event loop.
  - stop(): close the client and await the background task.
  - send(msg): fetch the user by ID and call user.send(text).
"""

import asyncio
import logging

import discord

from jarvis.channels.base import OutboundMessage
from jarvis.core.types import ChannelKind, ChannelMessage

_log = logging.getLogger(__name__)


class DiscordAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self, *, token: str, allowed_user_ids: set[str]) -> None:
        self._token = token
        self._allowed = set(allowed_user_ids)
        self._client: discord.Client | None = None
        self._task: asyncio.Task | None = None
        self._dispatcher = None  # set by start()
        self._ready = asyncio.Event()

    async def start(self, dispatcher) -> None:
        if self._client is not None:
            raise RuntimeError("DiscordAdapter already started")
        self._dispatcher = dispatcher
        self._ready.clear()

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            _log.info("discord adapter ready as %s", client.user)
            self._ready.set()

        @client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

        self._client = client
        self._task = asyncio.create_task(client.start(self._token), name="discord-adapter")
        # Wait for ready (or for the task to fail at login).
        ready_task = asyncio.create_task(self._ready.wait())
        done, pending = await asyncio.wait(
            {ready_task, self._task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._task in done and not self._ready.is_set():
            for p in pending:
                p.cancel()
            exc = self._task.exception()
            raise RuntimeError(f"discord login failed: {exc!r}") from exc
        for p in pending:
            if p is not self._task:
                p.cancel()

    async def stop(self) -> None:
        if self._client is None:
            return
        await self._client.close()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                _log.exception("discord client task ended with error")
        self._client = None
        self._task = None

    async def send(self, msg: OutboundMessage) -> None:
        if self._client is None:
            raise RuntimeError("DiscordAdapter not started")
        try:
            user_id = int(msg.channel_ref)
        except ValueError as e:
            raise ValueError(f"channel_ref {msg.channel_ref!r} is not a Discord user id") from e
        user = await self._client.fetch_user(user_id)
        await user.send(msg.text)

    async def _on_message(self, message: discord.Message) -> None:
        if not isinstance(message.channel, discord.DMChannel):
            return
        if message.author.bot:
            return
        author_id = str(message.author.id)
        if author_id not in self._allowed:
            return
        if self._dispatcher is None:
            _log.warning("discord on_message before dispatcher set; dropping")
            return

        ch_msg = ChannelMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref=author_id,
            text=message.content,
            external_id=str(message.id),
        )
        try:
            await self._dispatcher.dispatch_channel_message(ch_msg, allowed_refs=self._allowed)
        except Exception:
            _log.exception("discord dispatch failed")

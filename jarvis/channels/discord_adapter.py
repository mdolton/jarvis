"""DiscordAdapter — implements ChannelAdapter via discord.py.

Lifecycle:
  - start(dispatcher): begin a supervised connection loop and wait until the
    gateway is ready (or a short startup grace elapses).
  - stop(): signal the supervisor to exit and close the active client.
  - send(msg): fetch the user by ID and call user.send(text).

Supervision: the gateway connection runs under `_run_supervised`, which rebuilds
the client and reconnects (with capped exponential backoff) if `client.start`
ever returns or raises. discord.py reconnects internally, but if the event loop
is disturbed badly enough for the connection task to die, nothing else would
restart it — so the bot would go permanently deaf. The supervisor closes that
gap.
"""

import asyncio
import logging
from collections.abc import Callable

import discord
from discord import app_commands

from jarvis.channels.base import OutboundMessage
from jarvis.channels.discord_commands import ModelCommandDeps, register_model_commands
from jarvis.core.types import ChannelKind, ChannelMessage

_log = logging.getLogger(__name__)


def _default_client_factory() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    return discord.Client(intents=intents)


class DiscordAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(
        self,
        *,
        token: str,
        allowed_user_ids: set[str],
        model_command_deps: ModelCommandDeps | None = None,
        client_factory: Callable[[], discord.Client] | None = None,
        reconnect_backoff_base: float = 1.0,
        reconnect_backoff_max: float = 60.0,
        startup_grace_sec: float = 30.0,
    ) -> None:
        self._token = token
        self._allowed = set(allowed_user_ids)
        self._model_command_deps = model_command_deps
        self._client_factory = client_factory or _default_client_factory
        self._backoff_base = reconnect_backoff_base
        self._backoff_max = reconnect_backoff_max
        self._backoff = reconnect_backoff_base
        self._startup_grace = startup_grace_sec
        self._client: discord.Client | None = None
        self._tree: app_commands.CommandTree | None = None
        self._task: asyncio.Task | None = None
        self._dispatcher = None  # set by start()
        self._closing = False
        self._ready = asyncio.Event()

    async def start(self, dispatcher) -> None:
        if self._task is not None:
            raise RuntimeError("DiscordAdapter already started")
        self._dispatcher = dispatcher
        self._ready.clear()
        self._closing = False
        self._task = asyncio.create_task(self._run_supervised(), name="discord-adapter")

        # Wait until the gateway is ready, the supervisor dies, or the grace
        # window elapses — whichever comes first. We never block boot forever:
        # if Discord is unreachable the supervisor keeps retrying in the
        # background and the bot comes online once it can connect.
        ready_task = asyncio.create_task(self._ready.wait())
        done, pending = await asyncio.wait(
            {ready_task, self._task},
            timeout=self._startup_grace,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            if p is not self._task:
                p.cancel()

        if self._task in done and not self._ready.is_set():
            # Supervisor exited before ever connecting (only happens if it was
            # asked to stop). Surface any error.
            exc = self._task.exception()
            if exc is not None:
                raise RuntimeError(f"discord startup failed: {exc!r}") from exc
        elif not self._ready.is_set():
            _log.warning(
                "discord gateway not ready after %.0fs; continuing to retry in background",
                self._startup_grace,
            )

    async def _run_supervised(self) -> None:
        self._backoff = self._backoff_base
        while not self._closing:
            client = self._build_client()
            self._client = client
            try:
                await client.start(self._token)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("discord connection crashed; will restart")
            else:
                if not self._closing:
                    _log.warning("discord connection ended unexpectedly; will restart")
            finally:
                await self._safe_close(client)

            if self._closing:
                break
            await asyncio.sleep(self._backoff)
            self._backoff = (
                min(self._backoff * 2, self._backoff_max)
                if self._backoff > 0
                else self._backoff_base
            )

    def _build_client(self) -> discord.Client:
        client = self._client_factory()
        self._tree = None  # reset before possibly rebuilding for this client

        if self._model_command_deps is not None:
            tree = app_commands.CommandTree(client)
            register_model_commands(tree, allowed=self._allowed, deps=self._model_command_deps)
            self._tree = tree

        @client.event
        async def on_ready() -> None:
            _log.info("discord adapter ready as %s", client.user)
            self._backoff = self._backoff_base  # healthy connection; reset backoff
            self._ready.set()
            if self._tree is not None:
                try:
                    await self._tree.sync()
                except Exception:
                    _log.exception("failed to sync discord application commands")

        @client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

        return client

    async def _safe_close(self, client: discord.Client) -> None:
        try:
            if not client.is_closed():
                await client.close()
        except Exception:
            _log.exception("error closing discord client")

    async def stop(self) -> None:
        self._closing = True
        self._ready.clear()
        if self._client is not None:
            await self._safe_close(self._client)
        if self._task is not None:
            try:
                await self._task
            except Exception:
                _log.exception("discord supervisor task ended with error")
        self._client = None
        self._task = None

    def is_ready(self) -> bool:
        return self._ready.is_set()

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
            try:
                await message.channel.send(
                    "⚠ Couldn't process that. If it keeps happening, the selected "
                    "model may be unavailable — try changing it with `/model set`."
                )
            except Exception:
                _log.exception("failed to send discord error reply")

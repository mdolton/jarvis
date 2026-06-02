"""DiscordAdapter supervision: a crashing gateway connection must auto-restart.

Regression: previously `start()` scheduled `client.start(token)` as a bare
fire-and-forget task. If discord.py's connection died (e.g. the event loop was
disturbed and the gateway stopped responding), nothing restarted it and the bot
went permanently deaf. The adapter must supervise the connection and rebuild it.
"""

import asyncio

from jarvis.channels.discord_adapter import DiscordAdapter


class _StubDispatcher:
    async def dispatch_channel_message(self, msg, *, allowed_refs):
        return None


class _FakeClient:
    """A discord.Client stand-in driven by a per-instance behavior string."""

    def __init__(self, behavior: str) -> None:
        self._behavior = behavior
        self._handlers: dict[str, object] = {}
        self._closed = asyncio.Event()
        self.closed = False

    # discord.py registers handlers via the @client.event decorator.
    def event(self, fn):
        self._handlers[fn.__name__] = fn
        return fn

    async def start(self, token):
        if self._behavior == "crash":
            raise ConnectionResetError("gateway stopped responding")
        # "ready": announce readiness, then stay connected until closed.
        on_ready = self._handlers.get("on_ready")
        if on_ready is not None:
            await on_ready()
        await self._closed.wait()

    async def close(self):
        self.closed = True
        self._closed.set()

    def is_closed(self):
        return self.closed

    @property
    def user(self):
        return "fake-bot"


async def test_adapter_restarts_connection_after_unexpected_crash():
    """If the first connection crashes, the adapter rebuilds and reconnects."""
    behaviors = iter(["crash", "ready"])
    built: list[_FakeClient] = []

    def factory():
        client = _FakeClient(next(behaviors))
        built.append(client)
        return client

    adapter = DiscordAdapter(
        token="tok",
        allowed_user_ids={"1"},
        client_factory=factory,
        reconnect_backoff_base=0.0,
        reconnect_backoff_max=0.0,
    )

    # start() must not return until a connection has become ready, even though
    # the first attempt crashed.
    await asyncio.wait_for(adapter.start(_StubDispatcher()), timeout=2.0)
    try:
        assert len(built) == 2, "adapter did not rebuild the client after a crash"
        assert built[1].is_closed() is False
    finally:
        await adapter.stop()


async def test_stop_does_not_restart_the_connection():
    """After stop(), the supervisor must exit instead of reconnecting."""
    behaviors = iter(["ready", "ready", "ready"])
    built: list[_FakeClient] = []

    def factory():
        client = _FakeClient(next(behaviors))
        built.append(client)
        return client

    adapter = DiscordAdapter(
        token="tok",
        allowed_user_ids={"1"},
        client_factory=factory,
        reconnect_backoff_base=0.0,
        reconnect_backoff_max=0.0,
    )
    await asyncio.wait_for(adapter.start(_StubDispatcher()), timeout=2.0)
    await adapter.stop()

    # Give the supervisor a chance to (wrongly) loop, then confirm it didn't.
    await asyncio.sleep(0.05)
    assert len(built) == 1, "supervisor restarted the connection after stop()"

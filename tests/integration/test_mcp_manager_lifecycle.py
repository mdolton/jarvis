"""Regression tests for MCP connection lifecycle (single-owner-task invariant).

Background: `MCPServerStreamableHttp` is built on anyio, whose cancel scopes
must be entered AND exited on the same asyncio task. The original code closed
the previous OAuth connection from a fire-and-forget `asyncio.create_task`,
i.e. a *different* task than the one that opened it. That corrupted anyio's
cancel-scope state and tore down the whole event loop on every Gmail token
refresh (~hourly), eventually leaving the bot deaf.

These tests pin the invariant with a fake server that records which task
entered and which task exited it.
"""

import asyncio
import contextlib

import pytest

from jarvis.config.schema import MCPServersConfig
from jarvis.mcp.manager import MCPManager
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo


class _OkServer:
    """A well-behaved fake agents.mcp server."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_tools(self):
        return []


class _CancelOnCloseServer(_OkServer):
    """Fake server whose close raises CancelledError, mimicking the anyio
    cancellation that bleeds out of MCPServerStreamableHttp teardown."""

    async def __aexit__(self, *exc):
        raise asyncio.CancelledError("anyio scope teardown bled out")


class TaskTrackingServer:
    """Fake agents.mcp server that records the task it was entered/exited on.

    Mirrors the anyio invariant: a streamable-http connection's cancel scope
    must be exited on the same task that entered it.
    """

    def __init__(self) -> None:
        self.enter_task: asyncio.Task | None = None
        self.exit_task: asyncio.Task | None = None

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, *exc):
        self.exit_task = asyncio.current_task()

    async def list_tools(self):
        return []


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_replace_closes_old_server_on_same_task_that_opened_it(factory, monkeypatch):
    """The previous OAuth connection must be closed on the same task it was opened on."""
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        first = TaskTrackingServer()
        second = TaskTrackingServer()
        builds = iter([first, second])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name: next(builds),
        )

        await mgr.replace_oauth_server("gmail", url="x", headers={"Authorization": "Bearer A1"})
        await mgr.replace_oauth_server("gmail", url="x", headers={"Authorization": "Bearer A2"})
        # Let any background close settle (legacy code closed via create_task).
        await asyncio.sleep(0)

        assert first.exit_task is not None, "old server was never closed"
        assert first.enter_task is not None
        assert first.exit_task is first.enter_task, (
            "old MCP connection was closed on a different task than it was opened "
            "on — this is the anyio cancel-scope violation that crashed the loop"
        )
    finally:
        await mgr.stop()


async def test_stop_closes_server_opened_by_a_short_lived_task(factory, monkeypatch):
    """A connection opened inside an ephemeral task (the OAuth refresh job) must
    still be closed on the same owner task at shutdown — not on the task that
    happens to call stop()."""
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()

    sdk = TaskTrackingServer()
    monkeypatch.setattr(
        "jarvis.mcp.manager._build_streamable_http",
        lambda url, headers, *, name: sdk,
    )
    # Simulate the scheduler job: open the connection inside a task that then
    # completes, exactly like oauth_token_refresh -> replace_oauth_server.
    await asyncio.create_task(
        mgr.replace_oauth_server("gmail", url="x", headers={"Authorization": "Bearer A"})
    )

    # stop() runs on THIS task — different from the now-finished opening task.
    await mgr.stop()

    assert sdk.exit_task is not None, "server was never closed at stop()"
    assert sdk.exit_task is sdk.enter_task, (
        "connection closed on a different task than it was opened on during stop()"
    )


async def test_replace_survives_cancelled_error_from_old_connection_close(factory, monkeypatch):
    """A CancelledError bleeding out of the old connection's close must not kill
    the lifecycle task, hang the caller, or skip the DB status write.

    Reproduces the production 'Exception terminating connection ... CancelledError'
    seen when the OAuth refresh job swaps the Gmail MCP connection.
    """
    cfg = MCPServersConfig(servers=[])
    mgr = MCPManager(config=cfg, session_factory=factory)
    await mgr.start()
    try:
        old = _CancelOnCloseServer()
        new1 = _OkServer()
        new2 = _OkServer()
        builds = iter([old, new1, new2])
        monkeypatch.setattr(
            "jarvis.mcp.manager._build_streamable_http",
            lambda url, headers, *, name: next(builds),
        )

        await mgr.replace_oauth_server("gmail", url="x", headers={"Authorization": "Bearer A"})

        # Swapping closes `old`, whose __aexit__ raises CancelledError. The
        # replace must still complete, not hang and not kill the owner task.
        await asyncio.wait_for(
            mgr.replace_oauth_server("gmail", url="x", headers={"Authorization": "Bearer B"}),
            timeout=2.0,
        )
        assert mgr.agent_mcp_servers() == [new1]

        # The DB status write must have happened despite the bled cancellation.
        async with factory() as s:
            servers = {row.name: row.status for row in await MCPServerRepo(s).list_all()}
        assert servers.get("gmail") == "connected"

        # The owner task must still be alive: a subsequent replace works.
        await asyncio.wait_for(
            mgr.replace_oauth_server("gmail", url="x", headers={"Authorization": "Bearer C"}),
            timeout=2.0,
        )
        assert mgr.agent_mcp_servers() == [new2]
    finally:
        # Robust cleanup even if the owner task died (the bug under test).
        if mgr._loop_task is not None and not mgr._loop_task.done():
            await mgr.stop()
        elif mgr._loop_task is not None:
            mgr._loop_task.cancel()
            with contextlib.suppress(BaseException):
                await mgr._loop_task

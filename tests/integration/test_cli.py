"""CLI smoke tests via typer's CliRunner + patched AppContext."""

import pytest
from typer.testing import CliRunner

from jarvis import cli

# JARVIS_SECRETS_KEY is set for every integration test by the autouse
# `_set_secrets_key` fixture in tests/integration/conftest.py.


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "jarvis.yaml").write_text(
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: m
"""
    )
    (tmp_path / "channels.yaml").write_text("{}")
    (tmp_path / "mcp-servers.yaml").write_text("servers: []")
    return tmp_path


def test_check_config_prints_summary(config_dir):
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["check-config", "--config-dir", str(config_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "llm" in result.output.lower()
    assert "http://x/v1" in result.output


def test_invoke_requires_config_and_db(config_dir, tmp_path, monkeypatch):
    """The `invoke` command runs a manual dispatch and prints the output.

    We patch the LLM to avoid actually hitting a network endpoint.
    """
    # Monkeypatch AgentRunner.run to return a canned result.
    from jarvis.agents import runner as runner_mod

    async def _fake_run(self, request):
        from uuid import uuid4

        from jarvis.core.types import ChannelKind

        return runner_mod.AgentRunResult(
            final_output="FAKE-CLI-OUTPUT",
            conversation_id=uuid4(),
            trigger_id=uuid4(),
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="cli",
        )

    monkeypatch.setattr(runner_mod.AgentRunner, "run", _fake_run)

    db_path = tmp_path / "jarvis.db"
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "invoke",
            "hello",
            "--config-dir",
            str(config_dir),
            "--db-url",
            f"sqlite+aiosqlite:///{db_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "FAKE-CLI-OUTPUT" in result.output


def test_serve_starts_and_stops_cleanly(config_dir, tmp_path, monkeypatch):
    """The serve command bootstraps, waits, and shuts down on signal.

    We patch DiscordAdapter.start/stop to skip the network, and trigger
    shutdown by setting a stop_event from a background task.
    """
    from jarvis.channels import discord_adapter as da_mod
    from jarvis.cli import _serve_async

    started, stopped = [], []

    async def _fake_start(self, dispatcher):
        from unittest.mock import AsyncMock, MagicMock

        self._client = MagicMock()
        self._client.close = AsyncMock()
        started.append(self)

    async def _fake_stop(self):
        stopped.append(self)
        self._client = None

    monkeypatch.setattr(da_mod.DiscordAdapter, "start", _fake_start)
    monkeypatch.setattr(da_mod.DiscordAdapter, "stop", _fake_stop)

    # Add a discord channel config so an adapter actually gets created.
    (config_dir / "channels.yaml").write_text(
        'discord:\n  token: tok\n  allowed_user_ids: ["111"]\n'
    )

    db_path = tmp_path / "jarvis.db"

    import asyncio

    async def _drive() -> None:
        stop_event = asyncio.Event()

        async def _trigger_stop() -> None:
            await asyncio.sleep(0.05)
            stop_event.set()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_trigger_stop())
            tg.create_task(
                _serve_async(
                    config_dir=config_dir,
                    db_url=f"sqlite+aiosqlite:///{db_path}",
                    stop_event=stop_event,
                )
            )

    asyncio.run(_drive())

    assert len(started) == 1
    assert len(stopped) == 1

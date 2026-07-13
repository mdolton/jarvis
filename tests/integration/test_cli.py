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

    async def _fake_run(self, request, stream=None):
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

    import asyncio

    import uvicorn

    async def _fake_uvicorn_serve(self):
        return None

    monkeypatch.setattr(uvicorn.Server, "serve", _fake_uvicorn_serve)

    # Add a discord channel config so an adapter actually gets created.
    (config_dir / "channels.yaml").write_text(
        'discord:\n  token: tok\n  allowed_user_ids: ["111"]\n'
    )

    db_path = tmp_path / "jarvis.db"

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


def test_ingest_command_indexes_folder_and_is_idempotent(config_dir, tmp_path, monkeypatch):
    """`jarvis ingest` indexes a folder and skips unchanged files on re-run."""
    from jarvis.memory import embeddings as embeddings_mod

    vector = [1.0] + [0.0] * 1535  # matches the default embedding_dimensions

    async def _fake_embed(self, text):
        return list(vector)

    async def _fake_embed_many(self, texts):
        return [list(vector) for _ in texts]

    monkeypatch.setattr(embeddings_mod.OpenAIEmbeddingProvider, "embed", _fake_embed)
    monkeypatch.setattr(embeddings_mod.OpenAIEmbeddingProvider, "embed_many", _fake_embed_many)

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("the boiler pilot light reset code is 7-7-1")
    db_path = tmp_path / "jarvis.db"
    args = [
        "ingest",
        str(docs),
        "--config-dir",
        str(config_dir),
        "--db-url",
        f"sqlite+aiosqlite:///{db_path}",
    ]

    runner = CliRunner()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "created" in result.output

    again = runner.invoke(cli.app, args)
    assert again.exit_code == 0, again.output
    assert "unchanged" in again.output


def test_ingest_command_without_path_or_config_folder_errors(config_dir, tmp_path):
    db_path = tmp_path / "jarvis.db"
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["ingest", "--config-dir", str(config_dir), "--db-url", f"sqlite+aiosqlite:///{db_path}"],
    )
    assert result.exit_code == 2
    assert "documents_folder" in result.output


def test_serve_passes_forwarded_allow_ips_to_uvicorn(config_dir, tmp_path, monkeypatch):
    """The config's forwarded_allow_ips must reach uvicorn.Config — dropping
    it silently reverts to loopback trust and, behind a proxy, makes every
    request look like it comes from the proxy (or worse, spoofable)."""
    import asyncio

    import uvicorn

    (config_dir / "jarvis.yaml").write_text(
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: m
forwarded_allow_ips: "198.51.100.7"
"""
    )

    captured: dict = {}
    real_config = uvicorn.Config

    def _capturing_config(*args, **kwargs):
        captured.update(kwargs)
        return real_config(*args, **kwargs)

    monkeypatch.setattr(uvicorn, "Config", _capturing_config)

    async def _fake_uvicorn_serve(self):
        return None

    monkeypatch.setattr(uvicorn.Server, "serve", _fake_uvicorn_serve)

    from jarvis.cli import _serve_async

    async def _drive() -> None:
        stop_event = asyncio.Event()
        stop_event.set()  # shut down immediately after startup
        await _serve_async(
            config_dir=config_dir,
            db_url=f"sqlite+aiosqlite:///{tmp_path}/jarvis.db",
            stop_event=stop_event,
        )

    asyncio.run(_drive())
    assert captured["forwarded_allow_ips"] == "198.51.100.7"

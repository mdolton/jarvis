import pytest

from jarvis.main import bootstrap

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


async def test_bootstrap_loads_config_and_initializes_db(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.config.jarvis.llm.model == "m"
        assert db_path.exists()
    finally:
        await ctx.shutdown()


async def test_bootstrap_exposes_runner_and_dispatcher(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.agent_runner is not None
        assert ctx.dispatcher is not None
        assert ctx.mcp_manager is not None
    finally:
        await ctx.shutdown()


async def test_bootstrap_starts_discord_adapter_when_configured(tmp_path, monkeypatch):
    """When channels.yaml has discord, bootstrap should construct and start
    a DiscordAdapter. We patch DiscordAdapter.start to avoid a real connection."""
    from jarvis.channels import discord_adapter as da_mod

    started = []

    async def _fake_start(self, dispatcher):
        started.append(self)
        from unittest.mock import AsyncMock, MagicMock

        self._client = MagicMock()
        self._client.close = AsyncMock()

    async def _fake_stop(self):
        self._client = None

    monkeypatch.setattr(da_mod.DiscordAdapter, "start", _fake_start)
    monkeypatch.setattr(da_mod.DiscordAdapter, "stop", _fake_stop)

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "jarvis.yaml").write_text(
        "llm:\n  base_url: http://x/v1\n  api_key: x\n  model: m\n"
    )
    (config_dir / "channels.yaml").write_text(
        'discord:\n  token: tok\n  allowed_user_ids: ["111"]\n'
    )
    (config_dir / "mcp-servers.yaml").write_text("servers: []")

    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert len(ctx.channel_adapters) == 1
        assert ctx.output_router is not None
        assert len(started) == 1
    finally:
        await ctx.shutdown()


async def test_bootstrap_no_discord_when_unconfigured(tmp_path, config_dir):
    """The existing channels.yaml fixture has no discord — verify no adapter."""
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.channel_adapters == []
    finally:
        await ctx.shutdown()


async def test_bootstrap_exposes_scheduler(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.scheduler is not None
        assert ctx.scheduler.active_job_count() == 0  # no schedules in DB
    finally:
        await ctx.shutdown()


async def test_bootstrap_exposes_web_app(tmp_path, config_dir):
    from fastapi import FastAPI

    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.web_app is not None
        assert isinstance(ctx.web_app, FastAPI)
    finally:
        await ctx.shutdown()

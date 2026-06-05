import pytest
from fastapi import FastAPI

from jarvis.main import AppContext, bootstrap

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
        assert ctx.action_service is not None
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


async def test_bootstrap_exposes_model_components(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.llm_client is not None
        assert ctx.model_catalog is not None
        assert ctx.model_store is not None
        # Default selection is None -> current() equals the configured model.
        assert ctx.model_store.current() == ctx.config.jarvis.llm.model
    finally:
        await ctx.shutdown()


async def test_bootstrap_wires_memory_service_for_local_sqlite(tmp_path, config_dir):
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.memory_service is not None
        assert ctx.agent_runner._memory_service is ctx.memory_service
        assert ctx.action_service._memory_service is ctx.memory_service
        assert ctx.scheduler._runner._memory_service is ctx.memory_service
        assert ctx.memory_service._embedding_provider._client is ctx.llm_client
        assert ctx.memory_service._summarizer._client is ctx.llm_client
        assert ctx.memory_service._audit is ctx.audit
    finally:
        await ctx.shutdown()


async def test_bootstrap_disables_memory_recall_when_configured(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "jarvis.yaml").write_text(
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: m
memory:
  enabled: true
  recall_enabled: false
"""
    )
    (config_dir / "channels.yaml").write_text("{}")
    (config_dir / "mcp-servers.yaml").write_text("servers: []")

    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.memory_service is not None
        assert ctx.memory_service._max_recalled_memories == 0
    finally:
        await ctx.shutdown()


async def test_bootstrap_disables_memory_for_non_sqlite_db_url(tmp_path, config_dir, monkeypatch, caplog):
    from jarvis import main as main_mod
    from jarvis.persistence.db import create_engine as real_create_engine

    sqlite_url = f"sqlite+aiosqlite:///{tmp_path / 'jarvis.db'}"
    monkeypatch.setattr(main_mod, "create_engine", lambda db_url: real_create_engine(sqlite_url))

    caplog.set_level("WARNING")
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url="postgresql+asyncpg://jarvis:jarvis@localhost/jarvis",
    )
    try:
        assert ctx.memory_service is None
        assert ctx.agent_runner._memory_service is None
        assert ctx.action_service._memory_service is None
        assert ctx.scheduler._runner._memory_service is None
        assert "memory disabled" in caplog.text
    finally:
        await ctx.shutdown()


async def test_app_context_shutdown_drains_memory_tasks_before_closing_clients():
    events = []

    class FakeScheduler:
        async def stop(self):
            events.append("scheduler.stop")

    class FakeDrainable:
        def __init__(self, name):
            self.name = name

        async def drain_memory_tasks(self):
            events.append(f"{self.name}.drain")

    class FakeAdapter:
        async def stop(self):
            events.append("adapter.stop")

    class FakeStopper:
        def __init__(self, name):
            self.name = name

        async def stop(self):
            events.append(f"{self.name}.stop")

        async def aclose(self):
            events.append(f"{self.name}.aclose")

        async def close(self):
            events.append(f"{self.name}.close")

        async def dispose(self):
            events.append(f"{self.name}.dispose")

    ctx = AppContext(
        config=object(),
        engine=FakeStopper("engine"),
        session_factory=object(),
        audit=FakeStopper("audit"),
        mcp_manager=FakeStopper("mcp"),
        agent_runner=FakeDrainable("runner"),
        action_service=FakeDrainable("actions"),
        dispatcher=object(),
        channel_adapters=[FakeAdapter()],
        output_router=object(),
        scheduler=FakeScheduler(),
        web_app=FastAPI(),
        oauth_flow=object(),
        oauth_http=FakeStopper("oauth"),
        llm_client=FakeStopper("llm"),
        model_catalog=object(),
        model_store=object(),
        memory_service=None,
    )

    await ctx.shutdown()

    assert events.index("runner.drain") < events.index("llm.close")
    assert events.index("runner.drain") < events.index("audit.stop")
    assert events.index("runner.drain") < events.index("engine.dispose")
    assert events.index("actions.drain") < events.index("llm.close")
    assert events.index("actions.drain") < events.index("audit.stop")
    assert events.index("actions.drain") < events.index("engine.dispose")

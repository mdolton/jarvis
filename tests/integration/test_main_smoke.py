import pytest

from jarvis.main import bootstrap


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

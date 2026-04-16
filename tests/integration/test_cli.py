"""CLI smoke tests via typer's CliRunner + patched AppContext."""

import pytest
from typer.testing import CliRunner

from jarvis import cli


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

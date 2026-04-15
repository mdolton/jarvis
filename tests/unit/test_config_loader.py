from pathlib import Path

import pytest

from jarvis.config.loader import ConfigLoadError, expand_env, load_config


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_expand_env_substitutes_vars(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    assert expand_env({"token": "${MY_TOKEN}"}) == {"token": "s3cret"}


def test_expand_env_nested(monkeypatch):
    monkeypatch.setenv("X", "v")
    out = expand_env({"a": {"b": ["${X}", "plain"]}})
    assert out == {"a": {"b": ["v", "plain"]}}


def test_expand_env_missing_var_raises(monkeypatch):
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(ConfigLoadError, match="MISSING"):
        expand_env({"k": "${MISSING}"})


def test_load_config_reads_all_three_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")

    _write(
        tmp_path / "jarvis.yaml",
        """
llm:
  base_url: http://host.docker.internal:1234/v1
  api_key: dummy
  model: qwen
""",
    )
    _write(
        tmp_path / "channels.yaml",
        """
discord:
  token: ${DISCORD_TOKEN}
  allowed_user_ids: ["111"]
""",
    )
    _write(
        tmp_path / "mcp-servers.yaml",
        """
servers:
  - name: gcal
    transport: stdio
    command: ["python", "-m", "mcp_server_gcal"]
""",
    )

    cfg = load_config(tmp_path)
    assert cfg.jarvis.llm.model == "qwen"
    assert cfg.channels.discord is not None
    assert cfg.channels.discord.token == "tok"
    assert cfg.mcp_servers.servers[0].name == "gcal"


def test_load_config_missing_required_file(tmp_path):
    with pytest.raises(ConfigLoadError, match=r"jarvis\.yaml"):
        load_config(tmp_path)


def test_load_config_invalid_yaml(tmp_path):
    _write(tmp_path / "jarvis.yaml", "llm: [not valid")
    with pytest.raises(ConfigLoadError):
        load_config(tmp_path)

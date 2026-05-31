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


def _write_minimal_yaml(path):
    """Write the three required YAML config files to *path*."""
    _write(
        path / "jarvis.yaml",
        """
llm:
  base_url: http://host.docker.internal:1234/v1
  api_key: dummy
  model: qwen
""",
    )
    _write(path / "channels.yaml", "{}")
    _write(path / "mcp-servers.yaml", "servers: []")


def test_load_config_reads_all_three_files(tmp_path, monkeypatch, valid_fernet_key):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    monkeypatch.setenv("JARVIS_SECRETS_KEY", valid_fernet_key)

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


def test_load_config_reads_base_url_and_secrets_key(tmp_path, monkeypatch, valid_fernet_key):
    _write_minimal_yaml(tmp_path)
    monkeypatch.setenv("JARVIS_BASE_URL", "https://jarvis.example.com")
    monkeypatch.setenv("JARVIS_SECRETS_KEY", valid_fernet_key)
    cfg = load_config(tmp_path)
    assert cfg.base_url == "https://jarvis.example.com"
    assert cfg.secrets_key == valid_fernet_key.encode()


def test_load_config_base_url_defaults_to_localhost(tmp_path, monkeypatch, valid_fernet_key):
    _write_minimal_yaml(tmp_path)
    monkeypatch.delenv("JARVIS_BASE_URL", raising=False)
    monkeypatch.setenv("JARVIS_SECRETS_KEY", valid_fernet_key)
    cfg = load_config(tmp_path)
    assert cfg.base_url == "http://localhost:8080"


def test_load_config_secrets_key_missing_raises(tmp_path, monkeypatch):
    _write_minimal_yaml(tmp_path)
    monkeypatch.delenv("JARVIS_SECRETS_KEY", raising=False)
    from jarvis.oauth.crypto import SecretsKeyMissing

    with pytest.raises(SecretsKeyMissing):
        load_config(tmp_path)


def test_load_config_missing_required_file(tmp_path, monkeypatch, valid_fernet_key):
    monkeypatch.setenv("JARVIS_SECRETS_KEY", valid_fernet_key)
    with pytest.raises(ConfigLoadError, match=r"jarvis\.yaml"):
        load_config(tmp_path)


def test_load_config_invalid_yaml(tmp_path, monkeypatch, valid_fernet_key):
    monkeypatch.setenv("JARVIS_SECRETS_KEY", valid_fernet_key)
    _write(tmp_path / "jarvis.yaml", "llm: [not valid")
    with pytest.raises(ConfigLoadError):
        load_config(tmp_path)

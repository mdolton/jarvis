import pytest

from jarvis.config.schema import MCPServersConfig


def test_stdio_server_is_accepted():
    cfg = MCPServersConfig.model_validate(
        {"servers": [{"name": "fs", "transport": "stdio", "command": ["npx", "x"]}]}
    )
    assert cfg.servers[0].transport == "stdio"


def test_http_server_in_yaml_is_rejected():
    with pytest.raises(Exception) as ei:
        MCPServersConfig.model_validate(
            {"servers": [{"name": "r", "transport": "http", "url": "http://x"}]}
        )
    assert "stdio" in str(ei.value)

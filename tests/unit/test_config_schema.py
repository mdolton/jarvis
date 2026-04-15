import pytest
from pydantic import ValidationError

from jarvis.config.schema import (
    ChannelsConfig,
    DiscordChannelConfig,
    JarvisConfig,
    LLMConfig,
    MCPServerConfig,
    MCPServersConfig,
)


def test_jarvis_config_minimal():
    cfg = JarvisConfig(
        llm=LLMConfig(
            base_url="http://host.docker.internal:1234/v1",
            api_key="dummy",
            model="qwen2.5:32b",
        ),
    )
    assert cfg.idle_timeout_sec == 900  # default
    assert cfg.max_concurrent_agents == 3  # default
    assert cfg.timezone == "UTC"  # default


def test_jarvis_config_rejects_bad_output_fallback():
    with pytest.raises(ValidationError):
        JarvisConfig(
            llm=LLMConfig(base_url="x", api_key="x", model="x"),
            default_schedule_output_mode="pigeon",  # type: ignore[arg-type]
        )


def test_discord_channel_requires_token_and_allow_list():
    with pytest.raises(ValidationError):
        DiscordChannelConfig()  # type: ignore[call-arg]

    ok = DiscordChannelConfig(token="abc", allowed_user_ids=["1", "2"])
    assert ok.enabled is True
    assert len(ok.allowed_user_ids) == 2


def test_channels_config_discord_optional():
    cfg = ChannelsConfig()
    assert cfg.discord is None


def test_mcp_server_transport_validation():
    # stdio requires command
    with pytest.raises(ValidationError):
        MCPServerConfig(name="gcal", transport="stdio")  # type: ignore[call-arg]

    # http requires url
    with pytest.raises(ValidationError):
        MCPServerConfig(name="gcal", transport="http")  # type: ignore[call-arg]

    stdio_ok = MCPServerConfig(
        name="gcal",
        transport="stdio",
        command=["python", "-m", "mcp_server_gcal"],
    )
    assert stdio_ok.command[0] == "python"

    http_ok = MCPServerConfig(name="gcal", transport="http", url="http://x.local/mcp")
    assert http_ok.url == "http://x.local/mcp"


def test_mcp_servers_config_accepts_empty():
    cfg = MCPServersConfig(servers=[])
    assert cfg.servers == []

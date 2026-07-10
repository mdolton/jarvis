import pytest
from pydantic import ValidationError

from jarvis.config.schema import (
    ChannelsConfig,
    DiscordChannelConfig,
    JarvisConfig,
    LLMConfig,
    MCPServerConfig,
    MCPServersConfig,
    MemoryConfig,
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


def test_memory_config_defaults_are_enabled_and_deterministic():
    cfg = JarvisConfig(llm=LLMConfig(base_url="http://x/v1", api_key="k", model="m"))

    assert cfg.memory.enabled is True
    assert cfg.memory.recall_enabled is True
    assert cfg.memory.embedding_model is None
    assert cfg.memory.embedding_dimensions == 1536
    assert cfg.memory.max_recalled_memories == 5
    assert cfg.memory.min_relevance_score == 0.25


def test_memory_config_accepts_embedding_override():
    cfg = JarvisConfig(
        llm=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        memory={
            "embedding_model": "text-embedding-3-small",
            "embedding_dimensions": 768,
            "max_recalled_memories": 3,
            "min_relevance_score": 0.4,
        },
    )

    assert cfg.memory.embedding_model == "text-embedding-3-small"
    assert cfg.memory.embedding_dimensions == 768
    assert cfg.memory.max_recalled_memories == 3
    assert cfg.memory.min_relevance_score == 0.4


def test_memory_config_preference_dedup_defaults():
    cfg = MemoryConfig()

    assert cfg.preference_dedup_enabled is True
    assert cfg.preference_dup_high_threshold == 0.92
    assert cfg.preference_dup_low_threshold == 0.82
    assert cfg.preference_dedup_max_judge_calls == 5


def test_memory_config_rejects_out_of_range_threshold():
    with pytest.raises(ValidationError):
        MemoryConfig(preference_dup_high_threshold=1.5)


def test_memory_config_rejects_inverted_thresholds():
    with pytest.raises(ValidationError):
        MemoryConfig(
            preference_dup_low_threshold=0.95,
            preference_dup_high_threshold=0.92,
        )


def test_events_config_defaults_to_disabled():
    from jarvis.config.schema import EventsConfig

    cfg = EventsConfig()

    assert cfg.webhook_token is None
    assert cfg.coalesce_window_sec == 30.0


def test_events_config_rejects_negative_window():
    from jarvis.config.schema import EventsConfig

    with pytest.raises(ValidationError):
        EventsConfig(coalesce_window_sec=-1.0)


def test_memory_document_defaults():
    cfg = MemoryConfig()
    assert cfg.documents_folder is None
    assert cfg.document_chunk_chars == 1800
    assert cfg.document_chunk_overlap == 200
    assert cfg.max_document_results == 5


def test_document_overlap_must_be_smaller_than_chunk():
    with pytest.raises(ValidationError):
        MemoryConfig(document_chunk_chars=300, document_chunk_overlap=300)

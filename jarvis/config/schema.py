"""Pydantic schemas for YAML configs. Source of truth for file layout."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------
# jarvis.yaml
# --------------------------------------------------------------------

OutputMode = Literal["discord", "dashboard_only", "discord_if_noteworthy"]


class LLMConfig(_StrictModel):
    base_url: str
    api_key: str
    model: str
    request_timeout_sec: float = 60.0


class MemoryConfig(_StrictModel):
    enabled: bool = True
    recall_enabled: bool = True
    embedding_model: str | None = None
    embedding_dimensions: int = Field(default=1536, ge=1)
    max_recalled_memories: int = Field(default=5, ge=0, le=20)
    min_relevance_score: float = Field(default=0.25, ge=0.0)


class JarvisConfig(_StrictModel):
    llm: LLMConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    timezone: str = "UTC"
    idle_timeout_sec: int = 900
    max_concurrent_agents: int = 3
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    default_schedule_output_mode: OutputMode = "discord"


# --------------------------------------------------------------------
# channels.yaml
# --------------------------------------------------------------------


class DiscordChannelConfig(_StrictModel):
    token: str
    allowed_user_ids: list[str] = Field(min_length=1)
    enabled: bool = True


class ChannelsConfig(_StrictModel):
    discord: DiscordChannelConfig | None = None


# --------------------------------------------------------------------
# mcp-servers.yaml
# --------------------------------------------------------------------


class MCPServerConfig(_StrictModel):
    name: str
    transport: Literal["stdio", "http", "sse"]
    enabled: bool = True

    # stdio
    command: list[str] | None = None
    env: dict[str, str] | None = None

    # http / sse
    url: str | None = None
    headers: dict[str, str] | None = None

    @model_validator(mode="after")
    def _transport_fields_required(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires `command`")
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError(f"{self.transport} transport requires `url`")
        return self


class MCPServersConfig(_StrictModel):
    servers: list[MCPServerConfig] = Field(default_factory=list)

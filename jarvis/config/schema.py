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
    preference_dedup_enabled: bool = True
    preference_dup_high_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    preference_dup_low_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    preference_dedup_max_judge_calls: int = Field(default=5, ge=0)
    # Document corpus (notes, PDFs) ingestion + retrieval.
    documents_folder: str | None = None
    document_chunk_chars: int = Field(default=1800, ge=200)
    document_chunk_overlap: int = Field(default=200, ge=0)
    max_document_results: int = Field(default=5, ge=0, le=20)

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> "MemoryConfig":
        if self.preference_dup_low_threshold >= self.preference_dup_high_threshold:
            raise ValueError(
                "preference_dup_low_threshold must be strictly less than "
                "preference_dup_high_threshold"
            )
        if self.document_chunk_overlap >= self.document_chunk_chars:
            raise ValueError(
                "document_chunk_overlap must be strictly less than document_chunk_chars"
            )
        return self


class EventsConfig(_StrictModel):
    """Inbound event webhook (POST /events/webhook).

    No token → the endpoint is disabled and returns 404. The token gates who
    may wake an agent turn, so senders are operator-trusted; event *content*
    stays untrusted regardless (reduced tool scope + provenance tagging).
    """

    webhook_token: str | None = None
    coalesce_window_sec: float = Field(default=30.0, ge=0.0)


class JarvisConfig(_StrictModel):
    llm: LLMConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    timezone: str = "UTC"
    # Free-text home location ("Austin, Texas") surfaced to every agent run as
    # user context, so briefs can fetch a local weather forecast without a
    # hardcoded city. None → the location line is simply omitted.
    home_location: str | None = None
    idle_timeout_sec: int = 900
    max_concurrent_agents: int = 3
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    default_schedule_output_mode: OutputMode = "discord"
    # Max unsolicited pings per UTC day (P1 bypasses but still spends allowance);
    # sub-threshold notifications roll into the next scheduled digest.
    notification_daily_budget: int = Field(default=5, ge=1)


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
    # Operator assertion that every tool on this server only observes state.
    # Fills in a missing readOnlyHint annotation at tool discovery so pure-data
    # servers (weather, market quotes) stay callable on scheduled/event turns,
    # which deny anything not strictly read-only. It never downgrades a hint
    # the server itself provides.
    read_only: bool = False

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

    @model_validator(mode="after")
    def _only_stdio_in_yaml(self) -> "MCPServersConfig":
        for s in self.servers:
            if s.transport != "stdio":
                raise ValueError(
                    f"mcp-servers.yaml only supports stdio servers (server {s.name!r} "
                    f"uses {s.transport!r}); add http/sse servers via the dashboard"
                )
        return self

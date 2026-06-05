"""Core in-memory types shared across jarvis modules."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChannelKind(StrEnum):
    DISCORD = "discord"
    SCHEDULED = "scheduled"
    DASHBOARD = "dashboard"


class TriggerKind(StrEnum):
    DISCORD_MESSAGE = "discord_message"
    SCHEDULE = "schedule"
    MANUAL = "manual"


class AuditEventType(StrEnum):
    TRIGGER_RECEIVED = "trigger.received"
    SCHEDULE_FIRED = "schedule.fired"
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    LLM_ERROR = "llm.error"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"
    CHANNEL_SENT = "channel.sent"
    OUTPUT_SUPPRESSED = "output.suppressed"
    CONFIG_RELOAD_FAILED = "config.reload_failed"
    MODEL_CHANGED = "model.changed"
    MODEL_FALLBACK = "model.fallback"
    MCP_CONNECTED = "mcp.connected"
    MCP_DISCONNECTED = "mcp.disconnected"
    OAUTH_DISCOVERY_STARTED = "oauth.discovery_started"
    OAUTH_DISCOVERY_SUCCEEDED = "oauth.discovery_succeeded"
    OAUTH_DISCOVERY_FAILED = "oauth.discovery_failed"
    OAUTH_DCR_REGISTERED = "oauth.dcr_registered"
    OAUTH_CONSENT_REDIRECT_ISSUED = "oauth.consent_redirect_issued"
    OAUTH_CALLBACK_RECEIVED = "oauth.callback_received"
    OAUTH_STATE_MISMATCH = "oauth.state_mismatch"
    OAUTH_CONSENT_DECLINED = "oauth.consent_declined"
    OAUTH_TOKENS_OBTAINED = "oauth.tokens_obtained"
    OAUTH_REFRESH_SUCCEEDED = "oauth.refresh_succeeded"
    OAUTH_REFRESH_TRANSIENT_FAILURE = "oauth.refresh_transient_failure"
    OAUTH_REFRESH_PERMANENTLY_FAILED = "oauth.refresh_permanently_failed"
    OAUTH_REVOKED = "oauth.revoked"
    ACTION_CREATED = "action.created"
    ACTION_APPROVED = "action.approved"
    ACTION_REJECTED = "action.rejected"
    ACTION_COMPLETED = "action.completed"
    ACTION_FAILED = "action.failed"
    MEMORY_PREFERENCE_PROPOSED = "memory.preference_proposed"
    MEMORY_PREFERENCE_APPROVED = "memory.preference_approved"
    MEMORY_PREFERENCE_REJECTED = "memory.preference_rejected"
    MEMORY_ENTRY_CREATED = "memory.entry_created"
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_FAILED = "memory.failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class _ModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditEvent(_ModelBase):
    id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID | None = None
    trigger_id: UUID | None = None
    type: AuditEventType
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class ChannelMessage(_ModelBase):
    kind: Literal[TriggerKind.DISCORD_MESSAGE] = TriggerKind.DISCORD_MESSAGE
    channel_kind: ChannelKind
    channel_ref: str
    text: str
    external_id: str  # platform-native message id (for dedup)


class ScheduledTrigger(_ModelBase):
    kind: Literal[TriggerKind.SCHEDULE] = TriggerKind.SCHEDULE
    schedule_id: str
    prompt: str
    output_mode: Literal["discord", "dashboard_only", "discord_if_noteworthy"]
    model: str | None = None
    timezone: str | None = None
    fired_at: datetime | None = None


class ManualTrigger(_ModelBase):
    kind: Literal[TriggerKind.MANUAL] = TriggerKind.MANUAL
    user: str
    prompt: str


Trigger = Annotated[
    ChannelMessage | ScheduledTrigger | ManualTrigger,
    Field(discriminator="kind"),
]


class InvocationRequest(_ModelBase):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_utcnow)
    trigger: Trigger

"""SQLAlchemy ORM models. Column names match the design doc's data model."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jarvis.persistence.db import Base, TZDateTime


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    channel_kind: Mapped[str] = mapped_column(String(32))
    channel_ref: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(TZDateTime())
    last_activity_at: Mapped[datetime] = mapped_column(TZDateTime())
    status: Mapped[str] = mapped_column(String(16), default="open")
    idle_timeout_sec: Mapped[int | None] = mapped_column(default=None)

    __table_args__ = (
        Index(
            "ix_conversations_lookup",
            "channel_kind",
            "channel_ref",
            "status",
        ),
    )

    messages: Mapped[list["MessageRow"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # 'user' | 'assistant' | 'system'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())

    conversation: Mapped[ConversationRow] = relationship(back_populates="messages")


class TriggerRow(Base):
    __tablename__ = "triggers"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)


class ActionRow(Base):
    __tablename__ = "actions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(32), index=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel_kind: Mapped[str] = mapped_column(String(32))
    channel_ref: Mapped[str] = mapped_column(String(128))
    server_name: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict)
    run_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_item_json: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_actions_status_created_at", "status", "created_at"),
    )


class MemoryPreferenceRow(Base):
    __tablename__ = "memory_preferences"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text)
    content_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_memory_preferences_status_updated_at", "status", "updated_at"),
        Index(
            "ix_memory_preferences_content_normalized_unique",
            "content_normalized",
            unique=True,
        ),
    )


class MemoryEntryRow(Base):
    __tablename__ = "memory_entries"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_channel_kind: Mapped[str] = mapped_column(String(32))
    source_channel_ref: Mapped[str] = mapped_column(String(128))
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())
    last_recalled_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    evidence: Mapped[list["MemoryEvidenceRow"]] = relationship(
        back_populates="memory_entry", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_memory_entries_status_updated_at", "status", "updated_at"),
        Index("ix_memory_entries_source_hash_unique", "source_hash", unique=True),
    )


class MemoryEvidenceRow(Base):
    __tablename__ = "memory_evidence"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    memory_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_entries.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)

    memory_entry: Mapped[MemoryEntryRow] = relationship(back_populates="evidence")


class MemoryRecallEventRow(Base):
    __tablename__ = "memory_recall_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trigger_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    memory_entry_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("memory_entries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    score: Mapped[float] = mapped_column(Float)
    rank: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)


class ScheduleRow(Base):
    __tablename__ = "schedules"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    cron_expr: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    output_mode: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    discord_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notify_on_error: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())
    last_run_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DigestTemplateRow(Base):
    __tablename__ = "digest_templates"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    default_cron_expr: Mapped[str] = mapped_column(String(64))
    default_timezone: Mapped[str] = mapped_column(String(64))
    default_output_mode: Mapped[str] = mapped_column(String(32))
    default_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_discord_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    built_in: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    __table_args__ = (
        Index(
            "ix_digest_templates_enabled_category_name",
            "enabled",
            "category",
            "name",
        ),
    )


class MCPServerRow(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    transport: Mapped[str] = mapped_column(String(16))  # 'stdio' | 'http' | 'sse'
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    tools: Mapped[list["MCPToolRow"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class MCPToolRow(Base):
    __tablename__ = "mcp_tools"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    read_only_hint: Mapped[bool | None] = mapped_column(nullable=True)
    destructive_hint: Mapped[bool | None] = mapped_column(nullable=True)
    policy_override: Mapped[str | None] = mapped_column(String(16), nullable=True)

    server: Mapped[MCPServerRow] = relationship(back_populates="tools")


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)


class OAuthCredentialsRow(Base):
    __tablename__ = "oauth_credentials"

    provider_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id_enc: Mapped[bytes] = mapped_column(LargeBinary)
    client_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    scopes_granted: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="connected")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())


class OAuthPendingRow(Base):
    __tablename__ = "oauth_pending"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())

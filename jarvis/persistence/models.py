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

    __table_args__ = (Index("ix_actions_status_created_at", "status", "created_at"),)


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


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_type: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="indexing", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    chunks: Mapped[list["DocumentChunkRow"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_documents_source_ref_unique", "source_ref", unique=True),)


class DocumentChunkRow(Base):
    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)

    document: Mapped[DocumentRow] = relationship(back_populates="chunks")

    __table_args__ = (
        Index(
            "ix_document_chunks_document_chunk_unique",
            "document_id",
            "chunk_index",
            unique=True,
        ),
    )


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


class MCPProviderRow(Base):
    """Catalog entry: a secret-free service definition. stdio is NOT represented here."""

    __tablename__ = "mcp_providers"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # 'oauth' | 'http' | 'sse'
    mcp_url: Mapped[str] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(default=False)
    # oauth protocol facts (invariant across accounts)
    auth_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'dcr'|'manual'
    oauth_metadata_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pkce: Mapped[bool] = mapped_column(default=True)
    send_resource_indicator: Mapped[bool] = mapped_column(default=True)
    extra_auth_params: Mapped[dict] = mapped_column(JSON, default=dict)
    # non-authoritative form-prefill hints
    default_scopes: Mapped[list] = mapped_column(JSON, default=list)
    header_names: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    connections: Mapped[list["MCPConnectionRow"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class MCPConnectionRow(Base):
    """One credentialed account instance of a provider -> one live MCP server."""

    __tablename__ = "mcp_connections"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider_key: Mapped[str] = mapped_column(
        ForeignKey("mcp_providers.key", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(128))
    runtime_name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=True)
    # oauth client credentials (per connection; encrypted)
    client_id_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    client_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # oauth tokens (encrypted). access_token_enc IS NULL == registered but not authorized.
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    scopes_granted: Mapped[list] = mapped_column(JSON, default=list)
    # http/sse
    url_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # oauth credential/auth status (NOT the live-runtime status, which lives on MCPServerRow)
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    __table_args__ = (Index("ix_mcp_connections_runtime_name_unique", "runtime_name", unique=True),)

    provider: Mapped[MCPProviderRow] = relationship(back_populates="connections")


class MCPPendingRow(Base):
    __tablename__ = "mcp_pending"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_connections.id", ondelete="CASCADE"), index=True
    )
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())


class MCPServerRow(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    transport: Mapped[str] = mapped_column(String(16))  # 'stdio' | 'http' | 'sse'
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="stdio")  # 'stdio' | 'connection'
    connection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("mcp_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )

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


class NotificationRow(Base):
    """One unsolicited outbound notification: sent immediately or queued for a digest.

    `status` lifecycle: 'sent' (delivered as a standalone ping — these rows are
    what the daily budget counts), 'queued' (sub-threshold; waiting for the next
    digest), 'digested' (delivered inside a digest at `digested_at`).
    """

    __tablename__ = "notifications"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    priority: Mapped[int] = mapped_column(Integer)  # 1 (interrupt now) .. 4 (digest-only)
    source: Mapped[str] = mapped_column(String(128))  # e.g. "event:email", "scheduled"
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)  # sent | queued | digested
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    digested_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    __table_args__ = (Index("ix_notifications_status_created_at", "status", "created_at"),)


class UserRow(Base):
    """A dashboard account. Existence is gated by the config allow-list."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    # WebAuthn user.id handle: random bytes fixed at creation. Never rotated
    # (rotating orphans discoverable credentials) and never derived from the
    # email or any other PII (W3C WebAuthn §14.6.1 is normative on this).
    user_handle: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    disabled_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class AuthCodeRow(Base):
    """One emailed login code. Stored SHA-256 hashed; consumed via atomic CAS."""

    __tablename__ = "auth_codes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    nonce_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    consumed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    requested_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(TZDateTime())


class SessionRow(Base):
    """A dashboard login session, looked up by SHA-256 token hash."""

    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime())
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    last_auth_at: Mapped[datetime] = mapped_column(TZDateTime())
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_sessions_token_hash_unique", "token_hash", unique=True),)


class WebAuthnCredentialRow(Base):
    """A registered passkey. credential_id is the authenticator-issued ID (base64url)."""

    __tablename__ = "webauthn_credentials"

    credential_id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[list | None] = mapped_column(JSON, nullable=True)
    aaguid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backup_eligible: Mapped[bool] = mapped_column(default=False)
    backup_state: Mapped[bool] = mapped_column(default=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)


class RecoveryCodeRow(Base):
    """One single-use recovery code. Stored SHA-256 hashed; consumed via atomic CAS."""

    __tablename__ = "recovery_codes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    consumed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

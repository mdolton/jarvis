"""SQLAlchemy ORM models. Column names match the design doc's data model."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, String, Text
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

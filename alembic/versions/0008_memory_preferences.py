"""add memory preferences and entries

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_preferences_status", "memory_preferences", ["status"])
    op.create_index("ix_memory_preferences_created_at", "memory_preferences", ["created_at"])
    op.create_index(
        "ix_memory_preferences_status_updated_at",
        "memory_preferences",
        ["status", "updated_at"],
    )

    op.create_table(
        "memory_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("source_channel_kind", sa.String(length=32), nullable=False),
        sa.Column("source_channel_ref", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_recalled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_entries_conversation_id", "memory_entries", ["conversation_id"])
    op.create_index("ix_memory_entries_status", "memory_entries", ["status"])
    op.create_index("ix_memory_entries_created_at", "memory_entries", ["created_at"])
    op.create_index(
        "ix_memory_entries_status_updated_at",
        "memory_entries",
        ["status", "updated_at"],
    )

    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_entry_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["memory_entry_id"], ["memory_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_evidence_memory_entry_id", "memory_evidence", ["memory_entry_id"])
    op.create_index("ix_memory_evidence_created_at", "memory_evidence", ["created_at"])

    op.create_table(
        "memory_recall_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        sa.Column("memory_entry_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["memory_entry_id"], ["memory_entries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_recall_events_conversation_id",
        "memory_recall_events",
        ["conversation_id"],
    )
    op.create_index("ix_memory_recall_events_trigger_id", "memory_recall_events", ["trigger_id"])
    op.create_index(
        "ix_memory_recall_events_memory_entry_id",
        "memory_recall_events",
        ["memory_entry_id"],
    )
    op.create_index("ix_memory_recall_events_created_at", "memory_recall_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_memory_recall_events_created_at", table_name="memory_recall_events")
    op.drop_index("ix_memory_recall_events_memory_entry_id", table_name="memory_recall_events")
    op.drop_index("ix_memory_recall_events_trigger_id", table_name="memory_recall_events")
    op.drop_index("ix_memory_recall_events_conversation_id", table_name="memory_recall_events")
    op.drop_table("memory_recall_events")

    op.drop_index("ix_memory_evidence_created_at", table_name="memory_evidence")
    op.drop_index("ix_memory_evidence_memory_entry_id", table_name="memory_evidence")
    op.drop_table("memory_evidence")

    op.drop_index("ix_memory_entries_status_updated_at", table_name="memory_entries")
    op.drop_index("ix_memory_entries_created_at", table_name="memory_entries")
    op.drop_index("ix_memory_entries_status", table_name="memory_entries")
    op.drop_index("ix_memory_entries_conversation_id", table_name="memory_entries")
    op.drop_table("memory_entries")

    op.drop_index("ix_memory_preferences_status_updated_at", table_name="memory_preferences")
    op.drop_index("ix_memory_preferences_created_at", table_name="memory_preferences")
    op.drop_index("ix_memory_preferences_status", table_name="memory_preferences")
    op.drop_table("memory_preferences")

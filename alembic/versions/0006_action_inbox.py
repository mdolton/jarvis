"""add action inbox

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        sa.Column("channel_kind", sa.String(length=32), nullable=False),
        sa.Column("channel_ref", sa.String(length=128), nullable=False),
        sa.Column("server_name", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=True),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("run_state_json", sa.JSON(), nullable=False),
        sa.Column("approval_item_json", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_id"], ["triggers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_actions_status", "actions", ["status"], unique=False)
    op.create_index("ix_actions_created_at", "actions", ["created_at"], unique=False)
    op.create_index(
        "ix_actions_status_created_at",
        "actions",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index("ix_actions_conversation_id", "actions", ["conversation_id"], unique=False)
    op.create_index("ix_actions_trigger_id", "actions", ["trigger_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_actions_trigger_id", table_name="actions")
    op.drop_index("ix_actions_conversation_id", table_name="actions")
    op.drop_index("ix_actions_status_created_at", table_name="actions")
    op.drop_index("ix_actions_created_at", table_name="actions")
    op.drop_index("ix_actions_status", table_name="actions")
    op.drop_table("actions")

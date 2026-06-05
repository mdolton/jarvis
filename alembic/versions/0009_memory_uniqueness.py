"""add durable memory uniqueness keys

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_preferences",
        sa.Column("content_normalized", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_memory_preferences_content_normalized_unique",
        "memory_preferences",
        ["content_normalized"],
        unique=True,
    )

    op.add_column(
        "memory_entries",
        sa.Column("source_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_memory_entries_source_hash_unique",
        "memory_entries",
        ["source_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_entries_source_hash_unique", table_name="memory_entries")
    op.drop_column("memory_entries", "source_hash")

    op.drop_index(
        "ix_memory_preferences_content_normalized_unique",
        table_name="memory_preferences",
    )
    op.drop_column("memory_preferences", "content_normalized")

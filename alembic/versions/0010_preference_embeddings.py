"""add preference embedding columns

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_preferences",
        sa.Column("embedding", sa.JSON(), nullable=True),
    )
    op.add_column(
        "memory_preferences",
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_preferences", "embedding_dimensions")
    op.drop_column("memory_preferences", "embedding")

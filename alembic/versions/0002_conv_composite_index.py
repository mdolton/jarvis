"""composite index for conversation lookup

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-15 14:02:51.635824

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_conversations_lookup",
        "conversations",
        ["channel_kind", "channel_ref", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_lookup", table_name="conversations")

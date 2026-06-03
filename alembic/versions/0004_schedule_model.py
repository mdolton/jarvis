"""add schedules.model

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(sa.Column("model", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("model")

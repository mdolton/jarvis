"""add schedules.mcp_servers: per-schedule MCP server allow-list

NULL means "every connected server", which is exactly the behavior every
existing row had before this column, so no backfill is needed.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(sa.Column("mcp_servers", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("mcp_servers")

"""add digest templates

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digest_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("default_cron_expr", sa.String(length=64), nullable=False),
        sa.Column("default_timezone", sa.String(length=64), nullable=False),
        sa.Column("default_output_mode", sa.String(length=32), nullable=False),
        sa.Column("default_model", sa.String(length=128), nullable=True),
        sa.Column("default_discord_user_id", sa.String(length=128), nullable=True),
        sa.Column("built_in", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_digest_templates_key"),
    )
    op.create_index(
        "ix_digest_templates_enabled_category_name",
        "digest_templates",
        ["enabled", "category", "name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_digest_templates_enabled_category_name", table_name="digest_templates")
    op.drop_table("digest_templates")

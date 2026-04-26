"""oauth tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_credentials",
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("client_id_enc", sa.LargeBinary(), nullable=False),
        sa.Column("client_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=False),
        sa.Column("scopes_granted", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("provider_key"),
    )
    op.create_table(
        "oauth_pending",
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )


def downgrade() -> None:
    op.drop_table("oauth_pending")
    op.drop_table("oauth_credentials")

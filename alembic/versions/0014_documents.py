"""add documents + document_chunks tables (user-content retrieval corpus)

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-09 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_created_at", "documents", ["created_at"])
    op.create_index("ix_documents_source_ref_unique", "documents", ["source_ref"], unique=True)

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index("ix_document_chunks_created_at", "document_chunks", ["created_at"])
    op.create_index(
        "ix_document_chunks_document_chunk_unique",
        "document_chunks",
        ["document_id", "chunk_index"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_document_chunk_unique", table_name="document_chunks")
    op.drop_index("ix_document_chunks_created_at", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_source_ref_unique", table_name="documents")
    op.drop_index("ix_documents_created_at", table_name="documents")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_table("documents")

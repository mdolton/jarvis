"""normalize connection uuids

Repair data written by migration 0011, which inserted mcp_connections ids via
``str(uuid.uuid4())`` — a 36-char dashed string. SQLAlchemy's ``Uuid`` type
stores/queries UUIDs on SQLite as 32-char hex *without* dashes, so every
primary-key lookup (``session.get``) missed those rows. Full-table scans worked
(the dashed text parses back to a UUID), but ``MCPConnectionRepo.get`` returned
``None``, surfacing as "no connection row" on every OAuth token refresh and
breaking in-place token/status updates for migrated connections.

Strip the dashes from the affected columns so the stored text matches the
canonical form the ORM reads and writes. Idempotent: rows already in hex form
(fresh 0011 runs after its fix, or runtime-created rows) contain no dashes and
are left untouched by the ``LIKE '%-%'`` guard.

Foreign keys are unenforced during alembic migrations (the migration engine
does not set ``PRAGMA foreign_keys=ON``), so the parent and child columns can be
rewritten independently without ordering constraints.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-13 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE mcp_connections SET id = replace(id, '-', '') WHERE id LIKE '%-%'"
    )
    op.execute(
        "UPDATE mcp_servers SET connection_id = replace(connection_id, '-', '') "
        "WHERE connection_id LIKE '%-%'"
    )
    op.execute(
        "UPDATE mcp_pending SET connection_id = replace(connection_id, '-', '') "
        "WHERE connection_id LIKE '%-%'"
    )


def downgrade() -> None:
    """No-op: the dashed and hex forms encode the same UUIDs; re-adding dashes
    would only reintroduce the bug, so there is nothing meaningful to revert."""

"""provider connection model

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-13 00:00:00.000000
"""

import json as _json
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.dialects import sqlite

import jarvis.persistence.db
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Definition-only seed; mirrors jarvis.oauth.catalog.SEED_PROVIDERS. Duplicated here so
# the migration is hermetic (migrations must not import app code that may drift). A unit
# test (test_migration_seed_matches_catalog) guards that they stay in sync.
_SEED = [
    dict(
        key="fastmail",
        display_name="Fastmail",
        kind="oauth",
        mcp_url="https://api.fastmail.com/mcp",
        auth_mode="dcr",
        oauth_metadata_url="https://api.fastmail.com/.well-known/oauth-authorization-server",
        default_scopes=[],
        extra_auth_params={},
    ),
    dict(
        key="gmail",
        display_name="Gmail",
        kind="oauth",
        mcp_url="https://gmailmcp.googleapis.com/mcp/v1",
        auth_mode="manual",
        oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        default_scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
    ),
    dict(
        key="calendar",
        display_name="Google Calendar",
        kind="oauth",
        mcp_url="https://calendarmcp.googleapis.com/mcp/v1",
        auth_mode="manual",
        oauth_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        default_scopes=[
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            "https://www.googleapis.com/auth/calendar.events.freebusy",
            "https://www.googleapis.com/auth/calendar.events.readonly",
        ],
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
    ),
]
_GOOGLE_PROVIDERS = {"gmail", "calendar"}


def upgrade() -> None:
    """Upgrade schema, then seed providers and migrate legacy oauth_credentials."""
    bind = op.get_bind()
    now = datetime.now(UTC)

    op.create_table(
        "mcp_providers",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mcp_url", sa.Text(), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("auth_mode", sa.String(length=16), nullable=True),
        sa.Column("oauth_metadata_url", sa.Text(), nullable=True),
        sa.Column("pkce", sa.Boolean(), nullable=False),
        sa.Column("send_resource_indicator", sa.Boolean(), nullable=False),
        sa.Column("extra_auth_params", sa.JSON(), nullable=False),
        sa.Column("default_scopes", sa.JSON(), nullable=False),
        sa.Column("header_names", sa.JSON(), nullable=False),
        sa.Column("created_at", jarvis.persistence.db.TZDateTime(), nullable=False),
        sa.Column("updated_at", jarvis.persistence.db.TZDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "mcp_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("runtime_name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("client_id_enc", sa.LargeBinary(), nullable=True),
        sa.Column("client_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", jarvis.persistence.db.TZDateTime(), nullable=True),
        sa.Column("scopes_granted", sa.JSON(), nullable=False),
        sa.Column("url_override", sa.Text(), nullable=True),
        sa.Column("headers_enc", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("connected_at", jarvis.persistence.db.TZDateTime(), nullable=True),
        sa.Column("created_at", jarvis.persistence.db.TZDateTime(), nullable=False),
        sa.Column("updated_at", jarvis.persistence.db.TZDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["provider_key"], ["mcp_providers.key"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mcp_connections_provider_key"), "mcp_connections", ["provider_key"], unique=False
    )
    op.create_index(
        "ix_mcp_connections_runtime_name_unique", "mcp_connections", ["runtime_name"], unique=True
    )
    op.create_table(
        "mcp_pending",
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", jarvis.persistence.db.TZDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["mcp_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_index(
        op.f("ix_mcp_pending_connection_id"), "mcp_pending", ["connection_id"], unique=False
    )

    # Add mcp_servers.source as NOT NULL: backfill existing rows via a temporary
    # server_default, then drop the default so the final schema matches the model
    # (the model declares no server_default, so leaving one would fail `alembic check`).
    with op.batch_alter_table("mcp_servers") as batch:
        batch.add_column(
            sa.Column("source", sa.String(length=16), nullable=False, server_default="stdio")
        )
        batch.add_column(sa.Column("connection_id", sa.Uuid(), nullable=True))
        batch.create_index(
            op.f("ix_mcp_servers_connection_id"), ["connection_id"], unique=False
        )
        batch.create_foreign_key(
            "fk_mcp_servers_connection_id_mcp_connections",
            "mcp_connections",
            ["connection_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.alter_column("source", server_default=None)

    # --- Data migration (existing deployments) ------------------------------------------

    # Seed builtin providers.
    for p in _SEED:
        bind.execute(
            sa.text(
                "INSERT INTO mcp_providers (key, display_name, kind, mcp_url, builtin, auth_mode, "
                "oauth_metadata_url, pkce, send_resource_indicator, extra_auth_params, "
                "default_scopes, header_names, created_at, updated_at) VALUES "
                "(:key, :display_name, :kind, :mcp_url, 1, :auth_mode, :oauth_metadata_url, 1, 1, "
                ":extra_auth_params, :default_scopes, '[]', :now, :now)"
            ),
            {
                "key": p["key"],
                "display_name": p["display_name"],
                "kind": p["kind"],
                "mcp_url": p["mcp_url"],
                "auth_mode": p["auth_mode"],
                "oauth_metadata_url": p["oauth_metadata_url"],
                "extra_auth_params": _json.dumps(p["extra_auth_params"]),
                "default_scopes": _json.dumps(p["default_scopes"]),
                "now": now,
            },
        )

    # Migrate existing oauth_credentials -> one 'default' connection each.
    insp = sa.inspect(bind)
    if "oauth_credentials" in insp.get_table_names():
        legacy = bind.execute(
            sa.text(
                "SELECT provider_key, client_id_enc, client_secret_enc, access_token_enc, "
                "refresh_token_enc, token_expires_at, scopes_granted, status, last_error, "
                "connected_at FROM oauth_credentials"
            )
        ).fetchall()
        scopes_by_key = {p["key"]: p["default_scopes"] for p in _SEED}
        for row in legacy:
            m = row._mapping
            granted = m["scopes_granted"]
            granted_json = granted if isinstance(granted, str) else _json.dumps(granted or [])
            bind.execute(
                sa.text(
                    "INSERT INTO mcp_connections (id, provider_key, label, runtime_name, enabled, "
                    "client_id_enc, client_secret_enc, scopes, access_token_enc, refresh_token_enc, "
                    "token_expires_at, scopes_granted, url_override, headers_enc, status, last_error, "
                    "connected_at, created_at, updated_at) VALUES "
                    "(:id, :pk, 'Default', :rt, 1, :cid, :sec, :scopes, :at, :rt_tok, :exp, :granted, "
                    "NULL, NULL, :status, :err, :conn_at, :now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "pk": m["provider_key"],
                    "rt": f"{m['provider_key']}:default",
                    "cid": m["client_id_enc"],
                    "sec": m["client_secret_enc"],
                    "scopes": _json.dumps(scopes_by_key.get(m["provider_key"], [])),
                    "at": m["access_token_enc"],
                    "rt_tok": m["refresh_token_enc"],
                    "exp": m["token_expires_at"],
                    "granted": granted_json,
                    "status": m["status"],
                    "err": m["last_error"],
                    "conn_at": m["connected_at"],
                    "now": now,
                },
            )

    # Import Google app creds from env once, onto the gmail/calendar default connections
    # that have no client yet (i.e. no legacy row supplied one).
    secrets_key = os.environ.get("JARVIS_SECRETS_KEY")
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    sec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if secrets_key and cid:
        f = Fernet(secrets_key.encode())
        cid_enc = f.encrypt(cid.encode())
        sec_enc = f.encrypt(sec.encode()) if sec else None
        for pkey in _GOOGLE_PROVIDERS:
            rt = f"{pkey}:default"
            existing = bind.execute(
                sa.text("SELECT id, client_id_enc FROM mcp_connections WHERE runtime_name = :rt"),
                {"rt": rt},
            ).fetchone()
            if existing is None:
                bind.execute(
                    sa.text(
                        "INSERT INTO mcp_connections (id, provider_key, label, runtime_name, "
                        "enabled, client_id_enc, client_secret_enc, scopes, scopes_granted, status, "
                        "created_at, updated_at) VALUES "
                        "(:id, :pk, 'Default', :rt, 1, :cid, :sec, '[]', '[]', 'disconnected', "
                        ":now, :now)"
                    ),
                    {"id": str(uuid.uuid4()), "pk": pkey, "rt": rt, "cid": cid_enc, "sec": sec_enc, "now": now},
                )
            elif existing._mapping["client_id_enc"] is None:
                bind.execute(
                    sa.text(
                        "UPDATE mcp_connections SET client_id_enc = :cid, client_secret_enc = :sec "
                        "WHERE id = :id"
                    ),
                    {"cid": cid_enc, "sec": sec_enc, "id": existing._mapping["id"]},
                )

    # Drop the legacy oauth tables last.
    op.drop_table("oauth_pending")
    op.drop_table("oauth_credentials")


def downgrade() -> None:
    """Downgrade schema. oauth tables are recreated empty (one-way data migration)."""
    with op.batch_alter_table("mcp_servers") as batch:
        batch.drop_constraint(
            "fk_mcp_servers_connection_id_mcp_connections", type_="foreignkey"
        )
        batch.drop_index(op.f("ix_mcp_servers_connection_id"))
        batch.drop_column("connection_id")
        batch.drop_column("source")
    op.create_table(
        "oauth_pending",
        sa.Column("state", sa.VARCHAR(length=64), nullable=False),
        sa.Column("provider_key", sa.VARCHAR(length=64), nullable=False),
        sa.Column("code_verifier", sa.VARCHAR(length=128), nullable=False),
        sa.Column("created_at", sa.DATETIME(), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_table(
        "oauth_credentials",
        sa.Column("provider_key", sa.VARCHAR(length=64), nullable=False),
        sa.Column("client_id_enc", sa.BLOB(), nullable=False),
        sa.Column("client_secret_enc", sa.BLOB(), nullable=True),
        sa.Column("access_token_enc", sa.BLOB(), nullable=False),
        sa.Column("refresh_token_enc", sa.BLOB(), nullable=True),
        sa.Column("token_expires_at", sa.DATETIME(), nullable=False),
        sa.Column("scopes_granted", sqlite.JSON(), nullable=False),
        sa.Column("status", sa.VARCHAR(length=32), nullable=False),
        sa.Column("last_error", sa.TEXT(), nullable=True),
        sa.Column("connected_at", sa.DATETIME(), nullable=False),
        sa.Column("updated_at", sa.DATETIME(), nullable=False),
        sa.PrimaryKeyConstraint("provider_key"),
    )
    op.drop_index(op.f("ix_mcp_pending_connection_id"), table_name="mcp_pending")
    op.drop_table("mcp_pending")
    op.drop_index("ix_mcp_connections_runtime_name_unique", table_name="mcp_connections")
    op.drop_index(op.f("ix_mcp_connections_provider_key"), table_name="mcp_connections")
    op.drop_table("mcp_connections")
    op.drop_table("mcp_providers")

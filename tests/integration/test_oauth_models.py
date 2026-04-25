"""ORM smoke for oauth_credentials and oauth_pending."""

from datetime import UTC, datetime

import pytest

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import OAuthCredentialsRow, OAuthPendingRow


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_oauth_credentials_roundtrip(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        row = OAuthCredentialsRow(
            provider_key="fastmail",
            client_id_enc=b"enc-client-id",
            client_secret_enc=b"enc-secret",
            access_token_enc=b"enc-access",
            refresh_token_enc=b"enc-refresh",
            token_expires_at=now,
            scopes_granted=["mail.read"],
            status="connected",
            last_error=None,
            connected_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.commit()

    async with factory() as session:
        from sqlalchemy import select
        got = (await session.execute(select(OAuthCredentialsRow))).scalar_one()
        assert got.provider_key == "fastmail"
        assert got.access_token_enc == b"enc-access"
        assert got.scopes_granted == ["mail.read"]
        assert got.status == "connected"


async def test_oauth_pending_roundtrip(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        row = OAuthPendingRow(
            state="abc123",
            provider_key="fastmail",
            code_verifier="verifier-xyz",
            created_at=now,
        )
        session.add(row)
        await session.commit()

    async with factory() as session:
        from sqlalchemy import select
        got = (await session.execute(select(OAuthPendingRow))).scalar_one()
        assert got.state == "abc123"
        assert got.code_verifier == "verifier-xyz"

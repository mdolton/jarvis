"""OAuth repos: CRUD + filter helpers."""

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.oauth.store import OAuthCredentialsRepo, OAuthPendingRepo
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    yield f
    await engine.dispose()


async def test_credentials_upsert_and_get(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid",
            client_secret_enc=b"cs",
            access_token_enc=b"at",
            refresh_token_enc=b"rt",
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=["s1"],
        )
        got = await repo.get("fastmail")
        assert got is not None
        assert got.access_token_enc == b"at"
        assert got.status == "connected"


async def test_credentials_upsert_overwrites_existing(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at1", refresh_token_enc=b"rt1",
            token_expires_at=now + timedelta(hours=1),
            scopes_granted=[],
        )
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at2", refresh_token_enc=b"rt2",
            token_expires_at=now + timedelta(hours=2),
            scopes_granted=["new"],
        )
        got = await repo.get("fastmail")
        assert got.access_token_enc == b"at2"
        assert got.scopes_granted == ["new"]


async def test_credentials_set_status(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at", refresh_token_enc=b"rt",
            token_expires_at=now, scopes_granted=[],
        )
        await repo.set_status("fastmail", status="needs_reauth", last_error="invalid_grant")
        got = await repo.get("fastmail")
        assert got.status == "needs_reauth"
        assert got.last_error == "invalid_grant"


async def test_credentials_update_tokens(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at1", refresh_token_enc=b"rt1",
            token_expires_at=now, scopes_granted=[],
        )
        # Mark as needs_reauth, then update_tokens should reset to connected.
        await repo.set_status("fastmail", status="needs_reauth", last_error="oops")
        new_expires = now + timedelta(hours=2)
        await repo.update_tokens(
            "fastmail",
            access_token_enc=b"at2",
            refresh_token_enc=b"rt2",
            token_expires_at=new_expires,
        )
        got = await repo.get("fastmail")
        assert got.access_token_enc == b"at2"
        assert got.refresh_token_enc == b"rt2"
        assert got.status == "connected"
        assert got.last_error is None


async def test_credentials_update_tokens_skips_refresh_when_none(factory):
    """If refresh_token_enc is None on update, keep the existing one."""
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at1", refresh_token_enc=b"rt-original",
            token_expires_at=now, scopes_granted=[],
        )
        await repo.update_tokens(
            "fastmail",
            access_token_enc=b"at2",
            refresh_token_enc=None,
            token_expires_at=now + timedelta(hours=1),
        )
        got = await repo.get("fastmail")
        assert got.access_token_enc == b"at2"
        assert got.refresh_token_enc == b"rt-original"  # preserved


async def test_credentials_update_tokens_missing_raises(factory):
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        with pytest.raises(LookupError):
            await repo.update_tokens(
                "fastmail",
                access_token_enc=b"x", refresh_token_enc=b"y",
                token_expires_at=datetime.now(UTC),
            )


async def test_credentials_delete(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="fastmail",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at", refresh_token_enc=None,
            token_expires_at=now, scopes_granted=[],
        )
        await repo.delete("fastmail")
        assert await repo.get("fastmail") is None


async def test_credentials_list_due_for_refresh(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="soon",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at", refresh_token_enc=b"rt",
            token_expires_at=now + timedelta(seconds=30),  # within 90s window
            scopes_granted=[],
        )
        await repo.upsert(
            provider_key="later",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at", refresh_token_enc=b"rt",
            token_expires_at=now + timedelta(hours=1),  # not yet due
            scopes_granted=[],
        )
        due = await repo.list_due_for_refresh(now=now, skew_seconds=90)
        keys = {row.provider_key for row in due}
        assert keys == {"soon"}


async def test_credentials_list_due_skips_needs_reauth(factory):
    """Rows in needs_reauth must NOT be returned by list_due_for_refresh."""
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        await repo.upsert(
            provider_key="broken",
            client_id_enc=b"cid", client_secret_enc=None,
            access_token_enc=b"at", refresh_token_enc=b"rt",
            token_expires_at=now,  # would be due
            scopes_granted=[],
        )
        await repo.set_status("broken", status="needs_reauth", last_error="x")
        due = await repo.list_due_for_refresh(now=now, skew_seconds=90)
        assert due == []


async def test_credentials_list_all(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthCredentialsRepo(session)
        for k in ("a", "b", "c"):
            await repo.upsert(
                provider_key=k,
                client_id_enc=b"cid", client_secret_enc=None,
                access_token_enc=b"at", refresh_token_enc=None,
                token_expires_at=now, scopes_granted=[],
            )
        all_rows = await repo.list_all()
        assert {r.provider_key for r in all_rows} == {"a", "b", "c"}


async def test_pending_insert_lookup_delete(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthPendingRepo(session)
        await repo.insert(state="s1", provider_key="fastmail", code_verifier="v1", now=now)
        got = await repo.get("s1")
        assert got is not None
        assert got.code_verifier == "v1"
        await repo.delete("s1")
        assert await repo.get("s1") is None


async def test_pending_sweep_expired(factory):
    now = datetime.now(UTC)
    async with factory() as session:
        repo = OAuthPendingRepo(session)
        await repo.insert(state="old", provider_key="fastmail", code_verifier="v",
                          now=now - timedelta(minutes=20))
        await repo.insert(state="new", provider_key="fastmail", code_verifier="v",
                          now=now - timedelta(seconds=30))
        deleted = await repo.sweep_expired(now=now, ttl_seconds=600)
        assert deleted == 1
        assert await repo.get("old") is None
        assert await repo.get("new") is not None

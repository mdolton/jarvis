import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.persistence.db import Base
from jarvis.persistence.repositories import AuthRepo


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/repo.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_get_or_create_user_is_idempotent_and_handle_never_rotates(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        created = await repo.get_or_create_user("me@example.com")
        assert isinstance(created.user_handle, bytes)
        assert len(created.user_handle) == 16
        # The handle is random, not derived from the email (W3C §14.6.1: no PII).
        assert created.user_handle != _hash("me@example.com").encode()[:16]
        assert created.created_at.tzinfo is not None

    async with session_factory() as session:
        again = await AuthRepo(session).get_or_create_user("me@example.com")
    assert again.id == created.id
    assert again.user_handle == created.user_handle


async def test_consume_auth_code_roundtrip(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("123456"),
            nonce_hash=_hash("nonce"),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ip="127.0.0.1",
        )
        assert await repo.consume_auth_code(_hash("123456"), nonce_hash=_hash("nonce")) == user.id
        # Second redemption of the same code must fail.
        assert await repo.consume_auth_code(_hash("123456"), nonce_hash=_hash("nonce")) is None


async def test_consume_auth_code_rejects_expired_and_unknown(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("stale"),
            nonce_hash=None,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        assert await repo.consume_auth_code(_hash("stale")) is None
        assert await repo.consume_auth_code(_hash("never-issued")) is None


async def test_consume_auth_code_requires_exact_nonce_match(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("bound"),
            nonce_hash=_hash("browser-a"),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        # Wrong nonce and missing nonce both miss; the exact nonce redeems.
        assert await repo.consume_auth_code(_hash("bound"), nonce_hash=_hash("browser-b")) is None
        assert await repo.consume_auth_code(_hash("bound")) is None
        assert (
            await repo.consume_auth_code(_hash("bound"), nonce_hash=_hash("browser-a")) == user.id
        )


async def test_replace_auth_code_invalidates_and_carries_attempts(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("old"),
            nonce_hash=_hash("nonce-old"),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        for _ in range(3):
            await repo.record_code_attempt(_hash("nonce-old"))

        replaced = await repo.replace_auth_code(
            user_id=user.id,
            code_hash=_hash("new"),
            nonce_hash=_hash("nonce-new"),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        # The old code is dead, and the guess budget did NOT reset.
        assert await repo.consume_auth_code(_hash("old"), nonce_hash=_hash("nonce-old")) is None
        assert replaced.attempts == 3


async def test_record_code_attempt_and_max_attempts_gate(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("guarded"),
            nonce_hash=_hash("nonce"),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert await repo.record_code_attempt(_hash("wrong-nonce")) is None
        for expected in (1, 2, 3):
            assert await repo.record_code_attempt(_hash("nonce")) == expected
        assert (
            await repo.consume_auth_code(
                _hash("guarded"), nonce_hash=_hash("nonce"), max_attempts=2
            )
            is None
        )
        assert (
            await repo.consume_auth_code(
                _hash("guarded"), nonce_hash=_hash("nonce"), max_attempts=3
            )
            == user.id
        )


async def test_concurrent_consume_auth_code_single_winner(session_factory):
    """Two simultaneous redemptions of one code: exactly one gets the user_id."""
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("race"),
            nonce_hash=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )

    async def attempt():
        async with session_factory() as session:
            return await AuthRepo(session).consume_auth_code(_hash("race"))

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results, key=lambda r: r is None) == [user.id, None]


async def test_session_lifecycle(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        created = await repo.create_session(
            user_id=user.id,
            token_hash=_hash("token"),
            expires_at=datetime.now(UTC) + timedelta(days=30),
            user_agent="pytest",
            ip="127.0.0.1",
        )
        assert created.last_auth_at.tzinfo is not None

        found = await repo.get_session_by_token_hash(_hash("token"))
        assert found is not None and found.id == created.id
        assert await repo.get_session_by_token_hash(_hash("wrong")) is None

        before = found.last_seen_at
        await repo.touch_session(created.id)
        touched = await repo.get_session_by_token_hash(_hash("token"))
        assert touched.last_seen_at >= before

        await repo.revoke_session(created.id)
        revoked = await repo.get_session_by_token_hash(_hash("token"))
        assert revoked.revoked_at is not None


async def test_revoke_all_sessions_for_user(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        other = await repo.get_or_create_user("other@example.com")
        for n in range(2):
            await repo.create_session(
                user_id=user.id,
                token_hash=_hash(f"mine-{n}"),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        await repo.create_session(
            user_id=other.id,
            token_hash=_hash("theirs"),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

        assert await repo.revoke_all_sessions_for_user(user.id) == 2
        # Already-revoked sessions are not counted again.
        assert await repo.revoke_all_sessions_for_user(user.id) == 0
        theirs = await repo.get_session_by_token_hash(_hash("theirs"))
        assert theirs.revoked_at is None


async def test_delete_expired_sessions_and_codes(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_session(
            user_id=user.id,
            token_hash=_hash("live"),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        await repo.create_session(
            user_id=user.id,
            token_hash=_hash("dead"),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("fresh"),
            nonce_hash=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("expired"),
            nonce_hash=None,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        await repo.create_auth_code(
            user_id=user.id,
            code_hash=_hash("used"),
            nonce_hash=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert await repo.consume_auth_code(_hash("used")) == user.id

        assert await repo.delete_expired_sessions_and_codes() == 3
        assert await repo.get_session_by_token_hash(_hash("live")) is not None
        assert await repo.get_session_by_token_hash(_hash("dead")) is None
        assert await repo.consume_auth_code(_hash("fresh")) == user.id


async def test_credential_crud(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.add_credential(
            credential_id="cred-1",
            user_id=user.id,
            public_key=b"\x01\x02",
            transports=["internal", "hybrid"],
            aaguid="0000-aaaa",
            backup_eligible=True,
            name="MacBook",
        )
        await repo.add_credential(credential_id="cred-2", user_id=user.id, public_key=b"\x03")

        found = await repo.get_credential("cred-1")
        assert found.public_key == b"\x01\x02"
        assert found.transports == ["internal", "hybrid"]
        assert found.sign_count == 0
        assert found.last_used_at is None

        listed = await repo.list_credentials_for_user(user.id)
        assert [c.credential_id for c in listed] == ["cred-1", "cred-2"]

        await repo.record_credential_use("cred-1", sign_count=7, backup_state=True)
        used = await repo.get_credential("cred-1")
        assert used.sign_count == 7
        assert used.backup_state is True
        assert used.last_used_at is not None

        await repo.rename_credential("cred-1", "MacBook Pro")
        assert (await repo.get_credential("cred-1")).name == "MacBook Pro"

        await repo.delete_credential("cred-2")
        assert await repo.get_credential("cred-2") is None


async def test_recovery_code_consume_is_single_use(session_factory):
    async with session_factory() as session:
        repo = AuthRepo(session)
        user = await repo.get_or_create_user("me@example.com")
        await repo.create_recovery_codes(user.id, [_hash("r1"), _hash("r2")])

        assert await repo.consume_recovery_code(user.id, _hash("r1")) is True
        assert await repo.consume_recovery_code(user.id, _hash("r1")) is False
        assert await repo.consume_recovery_code(user.id, _hash("unknown")) is False
        assert await repo.consume_recovery_code(user.id, _hash("r2")) is True

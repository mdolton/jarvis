import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.auth.sessions import SessionManager, hash_token
from jarvis.config.schema import AuthConfig
from jarvis.persistence.db import Base
from jarvis.persistence.models import SessionRow
from jarvis.persistence.repositories import AuthRepo


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/auth.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _manager(factory, **overrides) -> SessionManager:
    return SessionManager(session_factory=factory, config=AuthConfig(**overrides))


async def _user_id(factory):
    async with factory() as session:
        return (await AuthRepo(session).get_or_create_user("me@example.com")).id


async def _backdate_last_seen(factory, raw_token: str, *, days: float = 0, seconds: float = 0):
    async with factory() as session:
        await session.execute(
            update(SessionRow)
            .where(SessionRow.token_hash == hash_token(raw_token))
            .values(last_seen_at=datetime.now(UTC) - timedelta(days=days, seconds=seconds))
        )
        await session.commit()


async def test_issue_and_validate_roundtrip(factory):
    manager = _manager(factory)
    user_id = await _user_id(factory)
    raw = await manager.issue_session(user_id)
    user = await manager.validate(raw)
    assert user is not None and user.id == user_id
    assert await manager.validate("not-a-real-token") is None


async def test_cookie_name_tracks_secure_cookies(factory):
    assert _manager(factory, secure_cookies=True).cookie_name == "__Host-jarvis_session"
    assert _manager(factory, secure_cookies=False).cookie_name == "jarvis_session"


async def test_rotation_invalidates_old_token_and_updates_last_auth_at(factory):
    manager = _manager(factory)
    user_id = await _user_id(factory)
    raw = await manager.issue_session(user_id)

    async with factory() as session:
        before = await AuthRepo(session).get_session_by_token_hash(hash_token(raw))

    new_raw = await manager.rotate_session(raw)
    assert new_raw is not None and new_raw != raw
    assert await manager.validate(raw) is None, "old token must die at rotation"
    assert await manager.validate(new_raw) is not None

    async with factory() as session:
        after = await AuthRepo(session).get_session_by_token_hash(hash_token(new_raw))
    assert after.id == before.id, "rotation swaps the token, not the session"
    assert after.last_auth_at >= before.last_auth_at

    # Rotating a token that no longer exists yields nothing.
    assert await manager.rotate_session(raw) is None


async def test_expired_session_rejected(factory):
    manager = _manager(factory)
    user_id = await _user_id(factory)
    async with factory() as session:
        await AuthRepo(session).create_session(
            user_id=user_id,
            token_hash=hash_token("expired-raw"),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    assert await manager.validate("expired-raw") is None


async def test_revoked_session_rejected(factory):
    manager = _manager(factory)
    user_id = await _user_id(factory)
    raw = await manager.issue_session(user_id)
    await manager.revoke(raw)
    assert await manager.validate(raw) is None


async def test_idle_timeout_rejected(factory):
    manager = _manager(factory, session_idle_timeout_days=7)
    user_id = await _user_id(factory)
    raw = await manager.issue_session(user_id)
    await _backdate_last_seen(factory, raw, days=8)
    assert await manager.validate(raw) is None


async def test_disabled_user_rejected(factory):
    manager = _manager(factory)
    user_id = await _user_id(factory)
    raw = await manager.issue_session(user_id)
    async with factory() as session:
        user = await AuthRepo(session).get_user(user_id)
        user.disabled_at = datetime.now(UTC)
        await session.commit()
    assert await manager.validate(raw) is None


async def test_touch_is_throttled(factory):
    manager = _manager(factory)
    user_id = await _user_id(factory)
    raw = await manager.issue_session(user_id)

    async def last_seen():
        async with factory() as session:
            row = await AuthRepo(session).get_session_by_token_hash(hash_token(raw))
            return row.last_seen_at

    fresh = await last_seen()
    await manager.validate(raw)
    assert await last_seen() == fresh, "a <60s-old last_seen_at must not be rewritten"

    await _backdate_last_seen(factory, raw, seconds=120)
    stale = await last_seen()
    await manager.validate(raw)
    assert await last_seen() > stale, "a stale last_seen_at is touched on use"


async def test_concurrent_rotation_single_winner(factory):
    manager = _manager(factory)
    user_id = await _user_id(factory)
    raw = await manager.issue_session(user_id)

    results = await asyncio.gather(manager.rotate_session(raw), manager.rotate_session(raw))
    assert sorted(r is None for r in results) == [False, True]

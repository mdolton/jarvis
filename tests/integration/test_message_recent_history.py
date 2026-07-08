"""MessageRepo.recent_history returns the newest N messages, chronological order."""

from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ConversationRepo, MessageRepo


async def _make_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, session_factory(engine)


async def test_recent_history_returns_newest_in_chronological_order(tmp_path):
    engine, factory = await _make_factory(tmp_path)
    try:
        async with factory() as session:
            conv = await ConversationRepo(session).find_or_create_open(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="42",
                idle_timeout_sec=900,
            )
            repo = MessageRepo(session)
            for i in range(5):
                role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
                await repo.append(conversation_id=conv.id, role=role, content=f"msg-{i}")

            recent = await repo.recent_history(conv.id, limit=3)

        assert [m.content for m in recent] == ["msg-2", "msg-3", "msg-4"]
    finally:
        await engine.dispose()


async def test_recent_history_empty_conversation(tmp_path):
    engine, factory = await _make_factory(tmp_path)
    try:
        async with factory() as session:
            conv = await ConversationRepo(session).find_or_create_open(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="42",
                idle_timeout_sec=900,
            )
            recent = await MessageRepo(session).recent_history(conv.id, limit=20)
        assert recent == []
    finally:
        await engine.dispose()

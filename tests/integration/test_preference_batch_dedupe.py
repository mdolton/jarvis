"""A batch with internally-duplicate normalized content persists one row, not zero."""

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MemoryPreferenceRepo, NewPreference


async def test_create_pending_many_with_internal_duplicate_persists_one(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    try:
        async with factory() as s:
            rows = await MemoryPreferenceRepo(s).create_pending_many(
                items=[
                    NewPreference(content="Prefer concise answers."),
                    NewPreference(content="prefer  concise answers."),  # same normalized
                ],
                source="agent_proposal",
            )
        assert len(rows) == 1
        assert rows[0].content == "Prefer concise answers."  # first occurrence wins

        async with factory() as s:
            stored = await MemoryPreferenceRepo(s).list_for_dedup()
        assert len(stored) == 1
    finally:
        await engine.dispose()

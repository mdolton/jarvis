import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.persistence.db import Base
from jarvis.persistence.repositories import DocumentRepo


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/repo.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_create_and_get_by_source_ref(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        created = await repo.create(
            source_type="file",
            source_ref="/notes/todo.md",
            title="todo",
            content_hash="a" * 64,
        )
    async with session_factory() as session:
        found = await DocumentRepo(session).get_by_source_ref("/notes/todo.md")
    assert found is not None
    assert found.id == created.id
    assert found.status == "indexing"
    assert found.created_at.tzinfo is not None


async def test_get_by_source_ref_misses_unknown(session_factory):
    async with session_factory() as session:
        assert await DocumentRepo(session).get_by_source_ref("/nope.md") is None


async def test_replace_chunks_swaps_content_and_reports_old_ids(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        doc = await repo.create(
            source_type="file", source_ref="/n.md", title="n", content_hash="b" * 64
        )
        _, first = await repo.replace_chunks(doc.id, ["one", "two"])
        old_ids, second = await repo.replace_chunks(doc.id, ["three"])

    assert old_ids == [row.id for row in first]
    assert [row.content for row in second] == ["three"]
    assert [row.chunk_index for row in second] == [0]
    async with session_factory() as session:
        assert await DocumentRepo(session).count_chunks(doc.id) == 1


async def test_set_status_and_mark_reingesting(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        doc = await repo.create(
            source_type="file", source_ref="/s.md", title="s", content_hash="c" * 64
        )
        await repo.set_status(doc.id, "unindexed", error="vec down")
        await repo.mark_reingesting(doc.id, title="s2", content_hash="d" * 64)
        refreshed = await repo.get_by_source_ref("/s.md")

    assert refreshed.status == "indexing"
    assert refreshed.error is None
    assert refreshed.title == "s2"
    assert refreshed.content_hash == "d" * 64


async def test_get_chunks_with_documents_filters_inactive(session_factory):
    async with session_factory() as session:
        repo = DocumentRepo(session)
        active = await repo.create(
            source_type="file", source_ref="/a.md", title="a", content_hash="e" * 64
        )
        stale = await repo.create(
            source_type="file", source_ref="/b.md", title="b", content_hash="f" * 64
        )
        _, active_rows = await repo.replace_chunks(active.id, ["hello"])
        _, stale_rows = await repo.replace_chunks(stale.id, ["bye"])
        await repo.set_status(active.id, "active")
        await repo.set_status(stale.id, "unindexed")

        found = await repo.get_chunks_with_documents([active_rows[0].id, stale_rows[0].id])

    assert set(found) == {active_rows[0].id}
    chunk, document = found[active_rows[0].id]
    assert chunk.content == "hello"
    assert document.title == "a"


async def test_get_chunks_with_documents_empty_input(session_factory):
    async with session_factory() as session:
        assert await DocumentRepo(session).get_chunks_with_documents([]) == {}

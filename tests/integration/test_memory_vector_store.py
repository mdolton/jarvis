from uuid import uuid4

import pytest

from jarvis.memory.vector_store import MemoryVectorStore


@pytest.fixture
async def store(tmp_path):
    db_path = tmp_path / "vec.db"
    vector_store = MemoryVectorStore(db_path=db_path, dimensions=3)
    await vector_store.initialize()
    return vector_store


async def test_vector_store_upsert_and_search(store):
    first = uuid4()
    second = uuid4()

    await store.upsert(first, [0.1, 0.2, 0.3])
    await store.upsert(second, [0.9, 0.8, 0.7])

    results = await store.search([0.1, 0.2, 0.3], limit=2)

    assert [r.memory_entry_id for r in results] == [first, second]
    assert results[0].score >= results[1].score


async def test_vector_store_unavailable_is_reported(monkeypatch, tmp_path):
    def broken_load(_conn):
        raise RuntimeError("extension missing")

    monkeypatch.setattr("jarvis.memory.vector_store.sqlite_vec.load", broken_load)
    vector_store = MemoryVectorStore(db_path=tmp_path / "broken.db", dimensions=3)

    await vector_store.initialize()

    assert vector_store.available is False
    assert "extension missing" in vector_store.last_error

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

    assert [r.entry_id for r in results] == [first, second]
    assert results[0].score >= results[1].score


async def test_vector_store_replaces_existing_embedding(store):
    memory_entry_id = uuid4()
    other = uuid4()

    await store.upsert(memory_entry_id, [0.1, 0.2, 0.3])
    await store.upsert(other, [0.1, 0.2, 0.4])
    await store.upsert(memory_entry_id, [0.9, 0.8, 0.7])

    results = await store.search([0.9, 0.8, 0.7], limit=3)

    assert [r.entry_id for r in results].count(memory_entry_id) == 1
    assert results[0].entry_id == memory_entry_id


async def test_vector_store_recreates_dimension_mismatched_index(tmp_path):
    db_path = tmp_path / "vec.db"
    original = MemoryVectorStore(db_path=db_path, dimensions=1536)
    await original.initialize()

    resized = MemoryVectorStore(db_path=db_path, dimensions=1024)
    await resized.initialize()
    memory_entry_id = uuid4()

    await resized.upsert(memory_entry_id, [0.1] * 1024)
    results = await resized.search([0.1] * 1024, limit=1)

    assert resized.available is True
    assert results[0].entry_id == memory_entry_id


async def test_vector_store_unavailable_is_reported(monkeypatch, tmp_path):
    def broken_load(_conn):
        raise RuntimeError("extension missing")

    monkeypatch.setattr("jarvis.memory.vector_store.sqlite_vec.load", broken_load)
    vector_store = MemoryVectorStore(db_path=tmp_path / "broken.db", dimensions=3)

    await vector_store.initialize()

    assert vector_store.available is False
    assert "extension missing" in vector_store.last_error


def test_vector_store_rejects_non_positive_dimensions(tmp_path):
    with pytest.raises(ValueError, match="dimensions must be positive"):
        MemoryVectorStore(db_path=tmp_path / "bad.db", dimensions=0)


async def test_vector_store_initialize_closes_connection(monkeypatch, tmp_path):
    class FakeConnection:
        def __init__(self):
            self.closed = False

        def enable_load_extension(self, _enabled):
            pass

        def execute(self, _sql):
            return None

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    connections = []

    def connect(_db_path):
        conn = FakeConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr("jarvis.memory.vector_store.sqlite3.connect", connect)
    monkeypatch.setattr("jarvis.memory.vector_store.sqlite_vec.load", lambda _conn: None)
    vector_store = MemoryVectorStore(db_path=tmp_path / "vec.db", dimensions=3)

    await vector_store.initialize()

    assert connections
    assert all(conn.closed for conn in connections)


async def test_prefixed_store_is_isolated_from_default_store(tmp_path):
    db_path = tmp_path / "vec.db"
    memory_store = MemoryVectorStore(db_path=db_path, dimensions=3)
    document_store = MemoryVectorStore(db_path=db_path, dimensions=3, table_prefix="document")
    await memory_store.initialize()
    await document_store.initialize()

    memory_id = uuid4()
    chunk_id = uuid4()
    await memory_store.upsert(memory_id, [0.1, 0.2, 0.3])
    await document_store.upsert(chunk_id, [0.9, 0.8, 0.7])

    memory_results = await memory_store.search([0.1, 0.2, 0.3], limit=10)
    document_results = await document_store.search([0.9, 0.8, 0.7], limit=10)

    assert [r.entry_id for r in memory_results] == [memory_id]
    assert [r.entry_id for r in document_results] == [chunk_id]


async def test_delete_many_removes_entries(store):
    keep, drop = uuid4(), uuid4()
    await store.upsert(keep, [0.1, 0.2, 0.3])
    await store.upsert(drop, [0.9, 0.8, 0.7])

    await store.delete_many([drop, uuid4()])  # unknown ids are ignored

    results = await store.search([0.9, 0.8, 0.7], limit=10)
    assert [r.entry_id for r in results] == [keep]


def test_invalid_table_prefix_rejected(tmp_path):
    with pytest.raises(ValueError, match="table_prefix"):
        MemoryVectorStore(db_path=tmp_path / "x.db", dimensions=3, table_prefix="bad-prefix")

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


async def test_vector_store_replaces_existing_embedding(store):
    memory_entry_id = uuid4()
    other = uuid4()

    await store.upsert(memory_entry_id, [0.1, 0.2, 0.3])
    await store.upsert(other, [0.1, 0.2, 0.4])
    await store.upsert(memory_entry_id, [0.9, 0.8, 0.7])

    results = await store.search([0.9, 0.8, 0.7], limit=3)

    assert [r.memory_entry_id for r in results].count(memory_entry_id) == 1
    assert results[0].memory_entry_id == memory_entry_id


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

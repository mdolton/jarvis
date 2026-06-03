import pytest_asyncio

from jarvis.agents.model_store import ModelStore
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import SettingRow


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_current_falls_back_to_default_when_unset(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    assert store.selection() is None
    assert store.current() == "cfg-model"


async def test_set_specific_then_current(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    await store.set("gpt-4o")
    assert store.selection() == "gpt-4o"
    assert store.current() == "gpt-4o"


async def test_set_none_clears_override(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    await store.set("gpt-4o")
    await store.set(None)
    assert store.selection() is None
    assert store.current() == "cfg-model"


async def test_selection_persists_across_reload(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    await store.set("llama-3.1")

    store2 = ModelStore(session_factory=factory, default_model="cfg-model")
    await store2.load()
    assert store2.current() == "llama-3.1"


async def test_set_none_deletes_settings_row(factory):
    store = ModelStore(session_factory=factory, default_model="cfg-model")
    await store.load()
    await store.set("gpt-4o")
    await store.set(None)

    async with factory() as s:
        row = await s.get(SettingRow, "llm.active_model")
        assert row is None  # row removed, not stored as null

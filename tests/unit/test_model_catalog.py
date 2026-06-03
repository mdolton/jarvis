from types import SimpleNamespace

import pytest

from jarvis.agents.model_catalog import Catalog, ModelCatalog


class _FakeModels:
    def __init__(self, ids, *, raise_exc=None):
        self._ids = ids
        self._raise = raise_exc
        self.calls = 0

    async def list(self):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return SimpleNamespace(data=[SimpleNamespace(id=i) for i in self._ids])


class _FakeClient:
    def __init__(self, models):
        self.models = models


@pytest.mark.asyncio
async def test_list_models_returns_sorted_ok():
    client = _FakeClient(_FakeModels(["zeta", "alpha", "mid"]))
    cat = ModelCatalog(client)
    result = await cat.list_models()
    assert isinstance(result, Catalog)
    assert result.ok is True
    assert result.models == ["alpha", "mid", "zeta"]


@pytest.mark.asyncio
async def test_list_models_error_returns_not_ok_empty():
    client = _FakeClient(_FakeModels([], raise_exc=RuntimeError("boom")))
    cat = ModelCatalog(client)
    result = await cat.list_models()
    assert result.ok is False
    assert result.models == []


@pytest.mark.asyncio
async def test_success_is_cached_within_ttl_and_refetched_after():
    fake = _FakeModels(["a"])
    client = _FakeClient(fake)
    t = {"now": 1000.0}
    cat = ModelCatalog(client, ttl_sec=30.0, clock=lambda: t["now"])

    await cat.list_models()
    await cat.list_models()
    assert fake.calls == 1  # second served from cache

    t["now"] = 1031.0
    await cat.list_models()
    assert fake.calls == 2  # TTL expired -> refetch


@pytest.mark.asyncio
async def test_failure_is_not_cached():
    fake = _FakeModels([], raise_exc=RuntimeError("down"))
    client = _FakeClient(fake)
    t = {"now": 0.0}
    cat = ModelCatalog(client, ttl_sec=30.0, clock=lambda: t["now"])
    await cat.list_models()
    await cat.list_models()
    assert fake.calls == 2  # failures retried every call

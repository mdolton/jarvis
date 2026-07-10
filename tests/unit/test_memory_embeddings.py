from types import SimpleNamespace

from jarvis.memory.embeddings import OpenAIEmbeddingProvider


class _FakeEmbeddings:
    def __init__(self, batch_vectors=None):
        self.calls = []
        self._batch_vectors = batch_vectors or []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(kwargs["input"], list):
            # Return items out of order to prove we re-sort by index.
            data = [
                SimpleNamespace(embedding=vector, index=index)
                for index, vector in enumerate(self._batch_vectors)
            ]
            return SimpleNamespace(data=list(reversed(data)))
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])


class _FakeClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


async def test_openai_embedding_provider_uses_configured_model():
    client = _FakeClient()
    provider = OpenAIEmbeddingProvider(
        client=client,
        model="text-embedding-3-small",
        dimensions=3,
    )

    got = await provider.embed("hello memory")

    assert got == [0.1, 0.2, 0.3]
    assert client.embeddings.calls == [
        {
            "input": "hello memory",
            "model": "text-embedding-3-small",
            "dimensions": 3,
        }
    ]


async def test_embed_many_sends_batch_and_preserves_order():
    client = _FakeClient()
    client.embeddings = _FakeEmbeddings(batch_vectors=[[0.1, 0.2], [0.3, 0.4]])
    provider = OpenAIEmbeddingProvider(client=client, model="embed-model", dimensions=2)

    result = await provider.embed_many(["first", "second"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert client.embeddings.calls == [
        {"input": ["first", "second"], "model": "embed-model", "dimensions": 2}
    ]


async def test_embed_many_empty_input_short_circuits():
    client = _FakeClient()
    provider = OpenAIEmbeddingProvider(client=client, model="embed-model", dimensions=2)

    assert await provider.embed_many([]) == []
    assert client.embeddings.calls == []

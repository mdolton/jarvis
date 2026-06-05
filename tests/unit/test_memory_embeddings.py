from types import SimpleNamespace

from jarvis.memory.embeddings import OpenAIEmbeddingProvider


class _FakeEmbeddings:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
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

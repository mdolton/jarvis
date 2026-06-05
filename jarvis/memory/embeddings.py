from __future__ import annotations

from typing import Protocol

from openai import AsyncOpenAI


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class OpenAIEmbeddingProvider:
    def __init__(self, *, client: AsyncOpenAI, model: str, dimensions: int) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            input=text,
            model=self._model,
            dimensions=self._dimensions,
        )
        return list(response.data[0].embedding)

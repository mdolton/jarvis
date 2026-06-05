from __future__ import annotations

import json

from openai import AsyncOpenAI

from jarvis.memory.types import MemorySummary


class MemorySummarizer:
    def __init__(self, *, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def summarize(self, *, user_prompt: str, assistant_output: str) -> MemorySummary:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this Jarvis interaction for long-term recall. "
                        "Return strict JSON with keys summary, topics, entities, "
                        "evidence, preference_candidates. Evidence items need "
                        "kind, label, content."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"User prompt:\n{user_prompt}\n\n"
                        f"Assistant output:\n{assistant_output}"
                    ),
                },
            ],
        )
        text = response.choices[0].message.content
        data = json.loads(text)
        return MemorySummary(
            summary=str(data.get("summary", "")).strip(),
            topics=_string_list(data.get("topics")),
            entities=_string_list(data.get("entities")),
            evidence=_evidence_list(data.get("evidence")),
            preference_candidates=_string_list(data.get("preference_candidates")),
        )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (str(item).strip() for item in value) if item]


def _evidence_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    evidence = []
    for item in value:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        evidence.append(
            {
                "kind": str(item.get("kind", "")).strip(),
                "label": str(item.get("label", "")).strip(),
                "content": content,
            }
        )
    return evidence

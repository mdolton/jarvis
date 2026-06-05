from __future__ import annotations

import json
from json import JSONDecodeError

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
        text = _message_content(response)
        data = _loads_object(text)
        if data is None or not _has_required_fields(data):
            return _empty_summary()
        return MemorySummary(
            summary=str(data.get("summary", "")).strip(),
            topics=_string_list(data.get("topics")),
            entities=_string_list(data.get("entities")),
            evidence=_evidence_list(data.get("evidence")),
            preference_candidates=_string_list(data.get("preference_candidates")),
        )


def _message_content(response: object) -> str | None:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", None)


def _loads_object(text: str | None) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        return None

    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except (JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _json_candidates(text: str) -> list[str]:
    stripped = text.strip()
    candidates = [stripped]

    fence_start = stripped.find("```")
    if fence_start != -1:
        content_start = stripped.find("\n", fence_start)
        fence_end = stripped.find("```", content_start + 1)
        if content_start != -1 and fence_end != -1:
            candidates.append(stripped[content_start:fence_end].strip())

    object_start = stripped.find("{")
    object_end = stripped.rfind("}")
    if object_start != -1 and object_end > object_start:
        candidates.append(stripped[object_start : object_end + 1])

    return candidates


def _has_required_fields(data: dict) -> bool:
    return all(
        key in data
        for key in (
            "summary",
            "topics",
            "entities",
            "evidence",
            "preference_candidates",
        )
    )


def _empty_summary() -> MemorySummary:
    return MemorySummary(
        summary="",
        topics=[],
        entities=[],
        evidence=[],
        preference_candidates=[],
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

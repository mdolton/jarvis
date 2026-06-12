from __future__ import annotations

import json
import math
from json import JSONDecodeError
from typing import Protocol


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class JudgeProtocol(Protocol):
    async def judge(self, *, candidate: str, existing: str) -> bool: ...


class PreferenceJudge:
    """LLM tiebreak: is CANDIDATE already covered by EXISTING?"""

    def __init__(self, *, client, model: str) -> None:
        self._client = client
        self._model = model

    async def judge(self, *, candidate: str, existing: str) -> bool:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You deduplicate behavioral preference rules for an AI "
                            "assistant. Decide whether the CANDIDATE preference is "
                            "already covered by the EXISTING preference - i.e. the "
                            "same instruction (even if worded differently) or a "
                            "strict subset of it. Return strict JSON: "
                            '{"duplicate": true} or {"duplicate": false}.'
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"EXISTING:\n{existing}\n\nCANDIDATE:\n{candidate}",
                    },
                ],
            )
        except Exception:
            return False
        return _parse_duplicate(_message_content(response))


def _message_content(response: object) -> str | None:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", None)


def _parse_duplicate(text: str | None) -> bool:
    if not text:
        return False
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return False
    try:
        data = json.loads(cleaned[start : end + 1])
    except (JSONDecodeError, ValueError):
        return False
    return bool(data.get("duplicate")) if isinstance(data, dict) else False

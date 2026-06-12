from __future__ import annotations

import math
from typing import Protocol

from jarvis.memory.llm_json import loads_object, message_content


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
        return _parse_duplicate(message_content(response))


def _parse_duplicate(text: str | None) -> bool:
    data = loads_object(text)
    if not isinstance(data, dict):
        return False
    return bool(data.get("duplicate"))

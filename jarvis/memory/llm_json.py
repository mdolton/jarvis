from __future__ import annotations

import json
from json import JSONDecodeError


def message_content(response: object) -> str | None:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    return getattr(message, "content", None)


def loads_object(text: str | None) -> dict | None:
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

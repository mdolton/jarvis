from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from jarvis.memory.embeddings import EmbeddingProvider
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


@dataclass(frozen=True, slots=True)
class ExistingPreference:
    content: str
    embedding: list[float] | None
    embedding_dimensions: int | None
    status: str
    preference_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    matched_content: str
    matched_id: UUID | None
    score: float
    method: str  # "embedding" | "llm"


class _JudgeBudget:
    def __init__(self, limit: int) -> None:
        self._remaining = limit

    def available(self) -> bool:
        return self._remaining > 0

    def consume(self) -> None:
        self._remaining -= 1


class PreferenceDeduplicator:
    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        judge: JudgeProtocol,
        high_threshold: float,
        low_threshold: float,
        max_judge_calls: int,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._judge = judge
        self._high_threshold = high_threshold
        self._low_threshold = low_threshold
        self._max_judge_calls = max_judge_calls

    def new_budget(self) -> _JudgeBudget:
        return _JudgeBudget(self._max_judge_calls)

    async def embed(self, content: str) -> list[float] | None:
        try:
            return await self._embedding_provider.embed(content)
        except Exception:
            return None

    async def is_duplicate(
        self,
        *,
        candidate_content: str,
        candidate_embedding: list[float] | None,
        existing: list[ExistingPreference],
        judge_budget: _JudgeBudget,
    ) -> DuplicateMatch | None:
        if not candidate_embedding:  # None or empty list -> treat as missing
            return None
        best_score = -1.0
        best_pref: ExistingPreference | None = None
        for pref in existing:
            if not pref.embedding:  # None or empty list -> skip
                continue
            if pref.embedding_dimensions != len(candidate_embedding):
                continue
            score = cosine(candidate_embedding, pref.embedding)
            if score > best_score:
                best_score = score
                best_pref = pref
        if best_pref is None:
            return None
        if best_score >= self._high_threshold:
            return DuplicateMatch(best_pref.content, best_pref.preference_id, best_score, "embedding")
        if best_score >= self._low_threshold and judge_budget.available():
            judge_budget.consume()
            if await self._judge.judge(candidate=candidate_content, existing=best_pref.content):
                return DuplicateMatch(best_pref.content, best_pref.preference_id, best_score, "llm")
        return None

    async def cluster(
        self, preferences: list[ClusterPreference]
    ) -> list[list[ClusterPreference]]:
        n = len(preferences)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        budget = self.new_budget()
        for i in range(n):
            for j in range(i + 1, n):
                a, b = preferences[i], preferences[j]
                if not a.embedding or not b.embedding:
                    continue
                if a.embedding_dimensions != b.embedding_dimensions:
                    continue
                score = cosine(a.embedding, b.embedding)
                connected = False
                if score >= self._high_threshold:
                    connected = True
                # Best-effort tiebreak: the judge is directional (is a covered by b?),
                # but we treat duplication as symmetric here. Checking one ordering keeps
                # the per-run judge budget bounded; the high-threshold path catches the
                # clear duplicates regardless of order.
                elif score >= self._low_threshold and budget.available():
                    budget.consume()
                    connected = await self._judge.judge(candidate=a.content, existing=b.content)
                if connected:
                    union(i, j)

        groups: dict[int, list[ClusterPreference]] = {}
        for idx in range(n):
            groups.setdefault(find(idx), []).append(preferences[idx])
        return [group for group in groups.values() if len(group) >= 2]


@dataclass(frozen=True, slots=True)
class ClusterPreference:
    preference_id: UUID
    content: str
    status: str
    created_at: datetime
    updated_at: datetime
    embedding: list[float] | None
    embedding_dimensions: int | None


def choose_keeper(group: list[ClusterPreference]) -> ClusterPreference:
    if not group:
        raise ValueError("choose_keeper requires a non-empty group")
    actives = [p for p in group if p.status == "active"]
    if actives:
        return min(actives, key=lambda p: p.created_at)
    return max(group, key=lambda p: p.updated_at)

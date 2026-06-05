from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class VectorSearchResult:
    memory_entry_id: UUID
    distance: float
    score: float


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    memory_entry_id: UUID
    summary: str
    topics: list[str]
    entities: list[str]
    evidence: list[dict[str, str]]
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class MemoryContext:
    preferences: list[str]
    recalled: list[RecalledMemory]
    recall_available: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemorySummary:
    summary: str
    topics: list[str]
    entities: list[str]
    evidence: list[dict[str, str]]
    preference_candidates: list[str]

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis.memory.preference_dedup import (
    ClusterPreference,
    ExistingPreference,
    PreferenceDeduplicator,
    PreferenceJudge,
    choose_keeper,
    cosine,
)


def test_cosine_identical_is_one():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_mismatched_or_empty_is_zero():
    assert cosine([1.0, 0.0], [1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


class _FakeChat:
    def __init__(self, content):
        self._content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class _FakeClient:
    def __init__(self, content):
        self.chat = SimpleNamespace(completions=_FakeChat(content))


async def test_judge_parses_true():
    client = _FakeClient('{"duplicate": true}')
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is True


async def test_judge_parses_fenced_false():
    client = _FakeClient('```json\n{"duplicate": false}\n```')
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is False


async def test_judge_returns_false_on_garbage():
    client = _FakeClient("not json at all")
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is False


async def test_judge_returns_false_on_error():
    class _Boom:
        async def create(self, **kwargs):
            raise RuntimeError("boom")

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Boom()))
    judge = PreferenceJudge(client=client, model="m")
    assert await judge.judge(candidate="a", existing="b") is False


class _RecordingJudge:
    def __init__(self, verdict: bool):
        self._verdict = verdict
        self.calls = 0

    async def judge(self, *, candidate: str, existing: str) -> bool:
        self.calls += 1
        return self._verdict


class _StubEmbeddings:
    def __init__(self, vector):
        self._vector = vector

    async def embed(self, text: str) -> list[float]:
        return list(self._vector)


def _dedup(judge, *, high=0.92, low=0.82):
    return PreferenceDeduplicator(
        embedding_provider=_StubEmbeddings([0.0]),
        judge=judge,
        high_threshold=high,
        low_threshold=low,
        max_judge_calls=5,
    )


async def test_is_duplicate_high_similarity_no_judge():
    judge = _RecordingJudge(False)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="Run tests", embedding=[1.0, 0.0], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="Run the tests",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is not None
    assert match.method == "embedding"
    assert judge.calls == 0


async def test_is_duplicate_band_consults_judge_yes():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="Run tests", embedding=[1.0, 0.6], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is not None
    assert match.method == "llm"
    assert judge.calls == 1


async def test_is_duplicate_band_judge_no_keeps():
    judge = _RecordingJudge(False)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="Run tests", embedding=[1.0, 0.6], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is None
    assert judge.calls == 1


async def test_is_duplicate_below_low_threshold_keeps():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="x", embedding=[0.0, 1.0], embedding_dimensions=2, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is None
    assert judge.calls == 0


async def test_is_duplicate_skips_dimension_mismatch():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    existing = [ExistingPreference(content="x", embedding=[1.0, 0.0, 0.0], embedding_dimensions=3, status="active", preference_id=None)]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is None


async def test_is_duplicate_none_candidate_embedding_keeps():
    dedup = _dedup(_RecordingJudge(True))
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=None,
        existing=[ExistingPreference(content="x", embedding=[1.0, 0.0], embedding_dimensions=2, status="active", preference_id=None)],
        judge_budget=dedup.new_budget(),
    )
    assert match is None


async def test_is_duplicate_picks_best_scoring_existing():
    judge = _RecordingJudge(False)
    dedup = _dedup(judge)
    existing = [
        ExistingPreference(content="A", embedding=[1.0, 0.6], embedding_dimensions=2, status="active"),  # ~0.857, in band
        ExistingPreference(content="B", embedding=[1.0, 0.0], embedding_dimensions=2, status="active"),  # 1.0, above high
    ]
    match = await dedup.is_duplicate(
        candidate_content="c",
        candidate_embedding=[1.0, 0.0],
        existing=existing,
        judge_budget=dedup.new_budget(),
    )
    assert match is not None
    assert match.method == "embedding"
    assert match.matched_content == "B"
    assert judge.calls == 0


async def test_judge_budget_caps_calls():
    judge = _RecordingJudge(False)
    dedup = PreferenceDeduplicator(
        embedding_provider=_StubEmbeddings([0.0]),
        judge=judge,
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=1,
    )
    budget = dedup.new_budget()
    existing = [ExistingPreference(content="x", embedding=[1.0, 0.6], embedding_dimensions=2, status="active", preference_id=None)]
    await dedup.is_duplicate(candidate_content="c1", candidate_embedding=[1.0, 0.0], existing=existing, judge_budget=budget)
    await dedup.is_duplicate(candidate_content="c2", candidate_embedding=[1.0, 0.0], existing=existing, judge_budget=budget)
    assert judge.calls == 1  # second call had no budget left


def _cp(content, vec, status="pending", created=1, updated=1):
    base = datetime(2026, 6, 1, tzinfo=UTC)
    return ClusterPreference(
        preference_id=uuid4(),
        content=content,
        status=status,
        created_at=base.replace(day=created),
        updated_at=base.replace(day=updated),
        embedding=vec,
        embedding_dimensions=len(vec) if vec else None,
    )


async def test_cluster_groups_high_similarity_pairs():
    judge = _RecordingJudge(False)
    dedup = _dedup(judge)
    prefs = [
        _cp("Run tests", [1.0, 0.0]),
        _cp("Run the tests", [1.0, 0.0]),
        _cp("Use dark mode", [0.0, 1.0]),
    ]
    groups = await dedup.cluster(prefs)
    assert len(groups) == 1
    assert {p.content for p in groups[0]} == {"Run tests", "Run the tests"}
    assert judge.calls == 0


async def test_cluster_uses_judge_in_band():
    judge = _RecordingJudge(True)
    dedup = _dedup(judge)
    prefs = [
        _cp("a", [1.0, 0.0]),
        _cp("b", [1.0, 0.6]),  # cosine ~0.857, in band
    ]
    groups = await dedup.cluster(prefs)
    assert len(groups) == 1
    assert judge.calls == 1


async def test_cluster_skips_dimension_mismatch():
    dedup = _dedup(_RecordingJudge(True))
    groups = await dedup.cluster([
        _cp("a", [1.0, 0.0]),
        _cp("b", [1.0, 0.0, 0.0]),
    ])
    assert groups == []


def test_choose_keeper_prefers_oldest_active():
    active_new = _cp("new", [1.0], status="active", created=5)
    active_old = _cp("old", [1.0], status="active", created=2)
    pending = _cp("pending", [1.0], status="pending", created=1)
    assert choose_keeper([active_new, active_old, pending]) is active_old


def test_choose_keeper_falls_back_to_most_recent_update():
    p1 = _cp("a", [1.0], status="pending", updated=2)
    p2 = _cp("b", [1.0], status="rejected", updated=9)
    assert choose_keeper([p1, p2]) is p2


def test_choose_keeper_rejects_empty_group():
    with pytest.raises(ValueError):
        choose_keeper([])

from types import SimpleNamespace

import pytest

from jarvis.memory.preference_dedup import (
    ExistingPreference,
    PreferenceDeduplicator,
    PreferenceJudge,
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

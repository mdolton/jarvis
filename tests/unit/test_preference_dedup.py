from types import SimpleNamespace

import pytest

from jarvis.memory.preference_dedup import PreferenceJudge, cosine


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

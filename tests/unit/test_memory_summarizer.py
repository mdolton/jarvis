import json
from types import SimpleNamespace

import pytest

from jarvis.memory.summarizer import MemorySummarizer


class _FakeCompletions:
    def __init__(self, text, *, choices=None):
        self.text = text
        self.choices = choices
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.choices is not None:
            return SimpleNamespace(choices=self.choices)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))]
        )


class _FakeChat:
    def __init__(self, text, *, choices=None):
        self.completions = _FakeCompletions(text, choices=choices)


class _FakeClient:
    def __init__(self, text, *, choices=None):
        self.chat = _FakeChat(text, choices=choices)


async def test_memory_summarizer_parses_chat_completion_json():
    text = json.dumps(
        {
            "summary": "We discussed Jarvis memory.",
            "topics": ["jarvis", "memory"],
            "entities": ["sqlite-vec"],
            "evidence": [
                {
                    "kind": "identifier",
                    "label": "library",
                    "content": "sqlite-vec",
                }
            ],
            "preference_candidates": ["Prefer concise answers."],
        }
    )
    client = _FakeClient(text)

    summary = await MemorySummarizer(client=client, model="m").summarize(
        user_prompt="let's add memory",
        assistant_output="sounds good",
    )

    assert summary.summary == "We discussed Jarvis memory."
    assert summary.topics == ["jarvis", "memory"]
    assert summary.entities == ["sqlite-vec"]
    assert summary.evidence == [
        {
            "kind": "identifier",
            "label": "library",
            "content": "sqlite-vec",
        }
    ]
    assert summary.preference_candidates == ["Prefer concise answers."]
    assert client.chat.completions.calls[0]["model"] == "m"


async def test_memory_summarizer_extracts_json_from_fenced_prose():
    client = _FakeClient(
        """
        Here is the summary:

        ```json
        {
          "summary": " We discussed Jarvis memory. ",
          "topics": ["jarvis", "memory"],
          "entities": ["sqlite-vec"],
          "evidence": [
            {"kind": "identifier", "label": "library", "content": "sqlite-vec"}
          ],
          "preference_candidates": [" Prefer concise answers. "]
        }
        ```
        Done.
        """
    )

    summary = await MemorySummarizer(client=client, model="m").summarize(
        user_prompt="let's add memory",
        assistant_output="sounds good",
    )

    assert summary.summary == "We discussed Jarvis memory."
    assert summary.topics == ["jarvis", "memory"]
    assert summary.entities == ["sqlite-vec"]
    assert summary.evidence == [
        {
            "kind": "identifier",
            "label": "library",
            "content": "sqlite-vec",
        }
    ]
    assert summary.preference_candidates == ["Prefer concise answers."]


@pytest.mark.parametrize(
    ("text", "choices"),
    [
        (None, None),
        ("", None),
        ("not json", None),
        ("[]", None),
        ('{"summary": "only summary"}', None),
        (None, []),
    ],
)
async def test_memory_summarizer_malformed_or_partial_content_returns_empty_summary(
    text, choices
):
    client = _FakeClient(text, choices=choices)

    summary = await MemorySummarizer(client=client, model="m").summarize(
        user_prompt="let's add memory",
        assistant_output="sounds good",
    )

    assert summary.summary == ""
    assert summary.topics == []
    assert summary.entities == []
    assert summary.evidence == []
    assert summary.preference_candidates == []

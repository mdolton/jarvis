import json
from types import SimpleNamespace

from jarvis.memory.summarizer import MemorySummarizer


class _FakeCompletions:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))]
        )


class _FakeChat:
    def __init__(self, text):
        self.completions = _FakeCompletions(text)


class _FakeClient:
    def __init__(self, text):
        self.chat = _FakeChat(text)


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


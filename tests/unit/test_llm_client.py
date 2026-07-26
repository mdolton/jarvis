from openai import AsyncOpenAI

from jarvis.agents.llm_client import build_llm_client
from jarvis.config.schema import LLMConfig


def test_build_llm_client_uses_configured_base_url():
    cfg = LLMConfig(
        base_url="http://host.docker.internal:1234/v1",
        api_key="dummy",
        model="qwen2.5:32b",
    )
    client = build_llm_client(cfg)
    assert isinstance(client, AsyncOpenAI)
    assert str(client.base_url) == "http://host.docker.internal:1234/v1/"


def test_build_llm_client_request_timeout_applied():
    cfg = LLMConfig(
        base_url="http://x/v1",
        api_key="k",
        model="m",
        request_timeout_sec=30.0,
    )
    client = build_llm_client(cfg)
    # In openai 2.31.0 the SDK stores timeout as a plain float (not an httpx
    # Timeout object), so we assert equality against the scalar value.
    assert client.timeout == 30.0


def test_build_llm_client_disables_retries():
    """A timed-out turn must fail once, not be resubmitted three times.

    The SDK default (max_retries=2) turns one slow generation into three full
    prompt submissions against an already-saturated endpoint, and 3x the
    request budget overruns the outer run_timeout_sec guard.
    """
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m")
    assert build_llm_client(cfg).max_retries == 0

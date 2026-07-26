"""The model retry policy: resample 5xx, never re-run a timed-out turn."""

import httpx
import pytest
from agents import ModelRetryNormalizedError, RetryPolicyContext, Runner
from agents.items import ModelResponse, Usage
from agents.models.interface import Model
from agents.run_internal.model_retry import _normalize_retry_error
from openai import APITimeoutError, BadRequestError, InternalServerError, RateLimitError
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from jarvis.agents.factory import build_agent, retry_on_server_error
from jarvis.config.schema import LLMConfig


def _ctx(**kwargs) -> RetryPolicyContext:
    return RetryPolicyContext(
        error=RuntimeError("x"),
        attempt=1,
        max_retries=1,
        stream=False,
        normalized=ModelRetryNormalizedError(**kwargs),
    )


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retried(status):
    assert retry_on_server_error(_ctx(status_code=status)) is True


@pytest.mark.parametrize("status", [400, 401, 404, 422, 429])
def test_client_errors_are_not_retried(status):
    """Including 429: a saturated local endpoint is not helped by resubmitting."""
    assert retry_on_server_error(_ctx(status_code=status)) is False


def test_timeout_is_not_retried_even_with_a_server_status():
    """A turn that blew the request budget will blow it again, and each attempt
    costs a full generation — the exact behavior disabled in build_llm_client."""
    assert retry_on_server_error(_ctx(is_timeout=True)) is False
    assert retry_on_server_error(_ctx(status_code=500, is_timeout=True)) is False


def test_network_error_is_not_retried():
    assert retry_on_server_error(_ctx(is_network_error=True)) is False


def test_error_without_status_is_not_retried():
    assert retry_on_server_error(_ctx()) is False


def _normalized(exc: Exception) -> RetryPolicyContext:
    return _ctx_from(_normalize_retry_error(exc, None))


def _ctx_from(normalized: ModelRetryNormalizedError) -> RetryPolicyContext:
    return RetryPolicyContext(
        error=RuntimeError("x"), attempt=1, max_retries=1, stream=False, normalized=normalized
    )


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "http://x/v1/chat/completions"))


def test_real_openai_exceptions_map_as_expected():
    """Guards against SDK drift in how errors normalize — the policy reads
    `status_code` / `is_timeout`, so a change in either silently breaks it."""
    server_error = InternalServerError(
        "Failed to parse input at pos 226", response=_response(500), body=None
    )
    assert retry_on_server_error(_normalized(server_error)) is True

    timeout = APITimeoutError(request=httpx.Request("POST", "http://x/v1/chat/completions"))
    assert retry_on_server_error(_normalized(timeout)) is False

    bad_request = BadRequestError("nope", response=_response(400), body=None)
    assert retry_on_server_error(_normalized(bad_request)) is False

    rate_limited = RateLimitError("slow down", response=_response(429), body=None)
    assert retry_on_server_error(_normalized(rate_limited)) is False


def test_build_agent_attaches_the_retry_policy():
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m")
    agent, _ = build_agent(llm_config=cfg, mcp_servers_provider=list)
    retry = agent.model_settings.retry
    assert retry is not None
    assert retry.max_retries == 1
    assert retry.policy is retry_on_server_error


class _FlakyModel(Model):
    """Raises a canned exception on the first call, then returns text."""

    def __init__(self, first_error: Exception) -> None:
        self._first_error = first_error
        self.calls = 0

    async def get_response(self, *a, **kw):
        self.calls += 1
        if self.calls == 1:
            raise self._first_error
        msg = ResponseOutputMessage(
            id="msg-1",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text="recovered", annotations=[])],
        )
        return ModelResponse(output=[msg], usage=Usage(), response_id=None)

    async def stream_response(self, *a, **kw):
        if False:
            yield None


def _agent_with(model: Model):
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m")
    agent, _ = build_agent(llm_config=cfg, mcp_servers_provider=list, explicit_model=model)
    return agent


async def test_a_500_is_resampled_and_the_run_completes():
    """The real-world case: llama.cpp 500s on a tool call it cannot parse, the
    resample comes back well-formed, and the scheduled run survives."""
    model = _FlakyModel(
        InternalServerError("Failed to parse input at pos 226", response=_response(500), body=None)
    )
    result = await Runner.run(_agent_with(model), "daily brief")
    assert model.calls == 2
    assert result.final_output == "recovered"


async def test_a_timeout_is_not_resampled():
    timeout = APITimeoutError(request=httpx.Request("POST", "http://x/v1/chat/completions"))
    model = _FlakyModel(timeout)
    with pytest.raises(APITimeoutError):
        await Runner.run(_agent_with(model), "daily brief")
    assert model.calls == 1

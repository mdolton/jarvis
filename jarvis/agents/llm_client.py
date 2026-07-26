"""LLM client builder.

Wraps the `openai.AsyncOpenAI` constructor with config-driven values and
installs the client as the Agents SDK default so every Runner.run() call
uses the configured endpoint without per-call plumbing.
"""

from agents import set_default_openai_api, set_default_openai_client
from openai import AsyncOpenAI

from jarvis.config.schema import LLMConfig


def build_llm_client(cfg: LLMConfig) -> AsyncOpenAI:
    """Build an AsyncOpenAI client pointed at the configured endpoint.

    Does NOT install it globally — use `install_as_default` for that. This
    split lets tests build a client without clobbering the process-wide
    Agents SDK default.
    """
    return AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout=cfg.request_timeout_sec,
        # No client-side retries. Scheduled runs are not streamed (see
        # OutputRouter.open_stream), so a request timeout means the endpoint
        # spent the full budget generating — resubmitting the identical prompt
        # to an already-saturated local server just triples the load and pushes
        # the run past the outer run_timeout_sec guard, where the real
        # APITimeoutError is replaced by a bare TimeoutError.
        max_retries=0,
    )


def install_as_default(client: AsyncOpenAI) -> None:
    """Install `client` as the Agents SDK's default OpenAI client.

    Also pin the default API to chat_completions. The Agents SDK defaults
    to the OpenAI Responses API, which most local OpenAI-compatible
    endpoints (LM Studio, Ollama, llama.cpp) don't fully implement —
    specifically the MCP tool-result hand-back shape, which surfaces as
    `invalid_union` on the `input` field. Chat completions works against
    every endpoint we care about.

    The default OpenAI tracing exporter is replaced by bootstrap() via
    set_trace_processors([JarvisTraceProcessor(...)]), so we do not need to
    disable tracing here — doing so would also silence our own tracer.
    """
    set_default_openai_client(client)
    set_default_openai_api("chat_completions")

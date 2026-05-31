"""Shared pytest fixtures."""

import pytest

from jarvis.oauth.crypto import generate_key


@pytest.fixture
def valid_fernet_key() -> str:
    """A freshly generated, validly-formatted Fernet key.

    Generated per use so no real or look-alike key is ever committed to source.
    Tests that need ``JARVIS_SECRETS_KEY`` set get it via the integration-level
    autouse ``_set_secrets_key`` fixture; tests that need the value directly
    request this fixture.
    """
    return generate_key()


@pytest.fixture(autouse=True)
def _reset_agents_sdk_globals():
    """Reset Agents SDK process-global state between tests.

    Several tests install a custom trace processor via `set_trace_processors`
    and/or replace the default OpenAI client via `set_default_openai_client`
    (transitively, via `bootstrap` and `install_as_default`). Without an
    autouse cleanup, the state leaks across tests and can cause order-
    dependent flakiness — e.g., a tracer installed in test A may receive
    events from test B and silently fail because B's audit logger is gone.
    """
    yield
    # Clear any installed trace processors so the next test starts clean.
    from agents import set_trace_processors

    set_trace_processors([])

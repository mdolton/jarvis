"""Integration-test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _set_secrets_key(monkeypatch, valid_fernet_key):
    """Ensure every integration test runs with a valid JARVIS_SECRETS_KEY set."""
    monkeypatch.setenv("JARVIS_SECRETS_KEY", valid_fernet_key)

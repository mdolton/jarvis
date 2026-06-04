"""Integration-test fixtures."""

import pytest

# Importing the models module registers every table on ``Base.metadata``. Many
# integration fixtures call ``Base.metadata.create_all`` directly; without this
# import the metadata can be empty (if no test has imported the models yet),
# producing an empty schema and "no such table" errors that depend on test
# ordering. conftest is imported before any test module, so this guarantees the
# tables are registered before the first ``create_all``. Mirrors the explicit
# import in ``alembic/env.py``.
from jarvis.persistence import models  # noqa: F401


@pytest.fixture(autouse=True)
def _set_secrets_key(monkeypatch, valid_fernet_key):
    """Ensure every integration test runs with a valid JARVIS_SECRETS_KEY set."""
    monkeypatch.setenv("JARVIS_SECRETS_KEY", valid_fernet_key)

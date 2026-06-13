"""OAuth provider catalog: presence, types, collision check."""

import pytest

from jarvis.oauth.catalog import (
    SEED_PROVIDERS,
    AuthMode,
    ProviderEntry,
    assert_no_yaml_collision,
    slug_label,
)


def test_fastmail_entry_present():
    entry = SEED_PROVIDERS["fastmail"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.DCR
    assert entry.mcp_url == "https://api.fastmail.com/mcp"
    assert entry.oauth_metadata_url is not None


def test_provider_entry_is_frozen():
    entry = SEED_PROVIDERS["fastmail"]
    with pytest.raises(AttributeError):  # FrozenInstanceError is a subclass of AttributeError
        entry.mcp_url = "x"  # type: ignore[misc]


def test_assert_no_yaml_collision_passes_with_disjoint_names():
    assert_no_yaml_collision(["filesystem", "remote-api"])  # does not raise


def test_assert_no_yaml_collision_raises_on_match():
    with pytest.raises(ValueError, match="fastmail"):
        assert_no_yaml_collision(["filesystem", "fastmail"])


def test_assert_no_yaml_collision_empty_list_ok():
    assert_no_yaml_collision([])  # does not raise


def test_gmail_entry_present():
    entry = SEED_PROVIDERS["gmail"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.MANUAL
    assert entry.mcp_url == "https://gmailmcp.googleapis.com/mcp/v1"
    assert entry.oauth_metadata_url == (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    assert entry.default_scopes == (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    )
    assert entry.extra_auth_params == {"access_type": "offline", "prompt": "consent"}
    assert entry.send_resource_indicator is True


def test_calendar_entry_present():
    entry = SEED_PROVIDERS["calendar"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.MANUAL
    assert entry.mcp_url == "https://calendarmcp.googleapis.com/mcp/v1"
    assert entry.oauth_metadata_url == (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    assert entry.default_scopes == (
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.freebusy",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    )
    assert entry.extra_auth_params == {"access_type": "offline", "prompt": "consent"}
    assert entry.send_resource_indicator is True


def test_provider_entry_defaults_for_manual_fields():
    entry = SEED_PROVIDERS["fastmail"]
    assert entry.send_resource_indicator is True


def test_fastmail_entry_still_present():
    entry = SEED_PROVIDERS["fastmail"]
    assert entry.auth_mode == AuthMode.DCR


def test_seed_providers_have_kind_and_default_scopes():
    cal = SEED_PROVIDERS["calendar"]
    assert cal.kind == "oauth"
    assert cal.default_scopes  # documented scope set
    assert cal.display_name == "Google Calendar"


def test_slug_label_lowercases_and_dashes():
    assert slug_label("Work Account!") == "work-account"
    assert slug_label("  Personal  ") == "personal"
    assert slug_label("a/b") == "a-b"


def test_migration_seed_matches_catalog():
    import importlib.util
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0011_provider_connection_model.py"
    )
    spec = importlib.util.spec_from_file_location("m0011", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mig_keys = {p["key"] for p in mod._SEED}
    assert mig_keys == set(SEED_PROVIDERS)
    for p in mod._SEED:
        entry = SEED_PROVIDERS[p["key"]]
        assert p["mcp_url"] == entry.mcp_url
        assert list(p["default_scopes"]) == list(entry.default_scopes)
        assert p["auth_mode"] == entry.auth_mode.value
        assert p["pkce"] == entry.pkce
        assert p["send_resource_indicator"] == entry.send_resource_indicator

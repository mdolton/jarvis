from jarvis.core.types import TriggerSource
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.tool_policy import (
    RuntimeToolDecision,
    ToolEffect,
    ToolPolicy,
    classify,
    classify_effect,
    runtime_decision,
)


def _desc(**kwargs) -> MCPToolDescriptor:
    defaults = {"name": "x", "input_schema": {}}
    defaults.update(kwargs)
    return MCPToolDescriptor(**defaults)


# --- classify_effect: blast radius + reversibility ---


def test_effect_reads():
    for name in (
        "get_thing",
        "list_things",
        "read_item",
        "search_docs",
        "fetch_url",
        "find_events",
        "query_db",
        "describe_table",
        "lookup_contact",
        "check_status",
        "download_file_content",
    ):
        assert classify_effect(_desc(name=name)) == ToolEffect.READ, name


def test_effect_reversible_mutations():
    for name in (
        "create_draft",
        "create_event",
        "add_to_library",
        "update_event",
        "set_reminder",
        "edit_note",
        "rename_file",
        "move_message",
        "copy_file",
        "label_message",
        "unlabel_thread",
        "archive_thread",
        "mark_read",
        "star_message",
        "upsert_row",
        "upload_image",
        "restore_item",
        "apply_sensitive_message_label",
    ):
        assert classify_effect(_desc(name=name)) == ToolEffect.REVERSIBLE, name


def test_effect_irreversible_or_outward():
    for name in (
        "send_email",
        "reply_to_thread",
        "forward_message",
        "publish_post",
        "post_comment",
        "share_file",
        "submit_form",
        "pay_invoice",
        "purchase_item",
        "order_pizza",
        "transfer_funds",
        "execute_query",
        "run_script",
        "delete_event",
        "remove_from_library",
        "drop_table",
        "purge_queue",
        "revoke_token",
        "cancel_subscription",
        "respond_to_event",
        "accept_invite",
        "decline_meeting",
        "notify_user",
        "invite_member",
        "grant_access",
        "sign_document",
    ):
        assert classify_effect(_desc(name=name)) == ToolEffect.IRREVERSIBLE, name


def test_effect_unknown_names():
    for name in ("do_thing", "brave_web_search", "frobnicate"):
        assert classify_effect(_desc(name=name)) == ToolEffect.UNKNOWN, name


def test_effect_camel_case_and_bare_verbs():
    assert classify_effect(_desc(name="createDraft")) == ToolEffect.REVERSIBLE
    assert classify_effect(_desc(name="sendEmail")) == ToolEffect.IRREVERSIBLE
    assert classify_effect(_desc(name="GET_Thing")) == ToolEffect.READ
    assert classify_effect(_desc(name="fetch")) == ToolEffect.READ


def test_effect_hints_beat_names():
    # destructive_hint wins over a reversible-looking name.
    assert classify_effect(_desc(name="create_x", destructive_hint=True)) == ToolEffect.IRREVERSIBLE
    # read_only_hint wins over an irreversible-looking name.
    assert classify_effect(_desc(name="send_probe", read_only_hint=True)) == ToolEffect.READ
    # destructive beats read_only when both are set.
    assert (
        classify_effect(_desc(name="x", read_only_hint=True, destructive_hint=True))
        == ToolEffect.IRREVERSIBLE
    )


# --- classify (policy mapping) ---


def test_user_override_wins():
    t = _desc(name="whatever", destructive_hint=True)
    assert classify(t, override="auto") == ToolPolicy.AUTO
    assert classify(t, override="confirm") == ToolPolicy.CONFIRM


def test_read_only_hint_auto():
    assert classify(_desc(name="fetch", read_only_hint=True)) == ToolPolicy.AUTO


def test_destructive_hint_confirm():
    assert classify(_desc(name="list_events", destructive_hint=True)) == ToolPolicy.CONFIRM


def test_destructive_wins_over_read_only():
    """If a tool is both read_only AND destructive, destructive wins."""
    t = _desc(name="x", read_only_hint=True, destructive_hint=True)
    assert classify(t) == ToolPolicy.CONFIRM


def test_reversible_mutations_auto_allow():
    for name in ("create_draft", "update_event", "add_label", "archive_thread"):
        assert classify(_desc(name=name)) == ToolPolicy.AUTO, name


def test_irreversible_and_unknown_confirm():
    for name in ("send_email", "delete_event", "pay_invoice", "do_thing"):
        assert classify(_desc(name=name)) == ToolPolicy.CONFIRM, name


def test_override_accepts_none():
    """override=None falls through to annotation/heuristic."""
    assert classify(_desc(name="get_x"), override=None) == ToolPolicy.AUTO


# --- runtime_decision ---


def test_runtime_override_allow_confirm_deny():
    t = _desc(name="delete_event", destructive_hint=True)
    assert runtime_decision(t, override="allow") == RuntimeToolDecision.ALLOW
    assert runtime_decision(t, override="confirm") == RuntimeToolDecision.CONFIRM
    assert runtime_decision(t, override="deny") == RuntimeToolDecision.DENY


def test_runtime_auto_detect_maps_classifier():
    assert runtime_decision(_desc(name="list_events"), override=None) == RuntimeToolDecision.ALLOW
    assert runtime_decision(_desc(name="send_email"), override=None) == RuntimeToolDecision.CONFIRM


def test_runtime_reversible_allows_irreversible_confirms():
    assert runtime_decision(_desc(name="create_draft")) == RuntimeToolDecision.ALLOW
    assert runtime_decision(_desc(name="send_email")) == RuntimeToolDecision.CONFIRM


def test_sensitive_escalates_reads_and_reversibles():
    assert (
        runtime_decision(_desc(name="get_message"), sensitive=True) == RuntimeToolDecision.CONFIRM
    )
    assert (
        runtime_decision(_desc(name="create_draft"), sensitive=True) == RuntimeToolDecision.CONFIRM
    )


def test_sensitive_beats_allow_override():
    t = _desc(name="create_draft")
    assert runtime_decision(t, override="allow", sensitive=True) == RuntimeToolDecision.CONFIRM


def test_sensitive_never_relaxes_deny():
    t = _desc(name="create_draft")
    assert runtime_decision(t, override="deny", sensitive=True) == RuntimeToolDecision.DENY


def test_non_user_source_still_denies_reversible_mutations():
    """Reversible ≠ read-only: the non-user scope stays strictly read-only."""
    for source in (TriggerSource.SCHEDULED, TriggerSource.EVENT):
        for name in (
            "create_draft",
            "update_event",
            "archive_thread",
            "send_email",
            "delete_event",
            "do_thing",
        ):
            assert (
                runtime_decision(_desc(name=name), trigger_source=source)
                == RuntimeToolDecision.DENY
            ), (source, name)


def test_non_user_source_denies_even_with_allow_or_confirm_override():
    """A per-tool override is set without trigger-source context; restriction wins."""
    t = _desc(name="send_email")
    for override in ("allow", "auto", "confirm"):
        assert (
            runtime_decision(t, override=override, trigger_source=TriggerSource.EVENT)
            == RuntimeToolDecision.DENY
        ), override


def test_non_user_source_denies_destructive_read_only():
    t = _desc(name="list_events", read_only_hint=True, destructive_hint=True)
    assert runtime_decision(t, trigger_source=TriggerSource.EVENT) == RuntimeToolDecision.DENY


def test_non_user_source_allows_read_only_tools():
    for source in (TriggerSource.SCHEDULED, TriggerSource.EVENT):
        assert (
            runtime_decision(_desc(name="list_events"), trigger_source=source)
            == RuntimeToolDecision.ALLOW
        )
        assert (
            runtime_decision(_desc(name="whoami", read_only_hint=True), trigger_source=source)
            == RuntimeToolDecision.ALLOW
        )


def test_non_user_source_keeps_deny_override():
    t = _desc(name="list_events")
    assert (
        runtime_decision(t, override="deny", trigger_source=TriggerSource.EVENT)
        == RuntimeToolDecision.DENY
    )


def test_user_source_behavior_unchanged():
    t = _desc(name="send_email")
    assert runtime_decision(t, trigger_source=TriggerSource.USER) == RuntimeToolDecision.CONFIRM
    assert (
        runtime_decision(t, override="allow", trigger_source=TriggerSource.USER)
        == RuntimeToolDecision.ALLOW
    )

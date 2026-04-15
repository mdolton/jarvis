from jarvis.core.types import MessageRole


def test_message_role_values():
    assert MessageRole.USER.value == "user"
    assert MessageRole.ASSISTANT.value == "assistant"
    assert MessageRole.SYSTEM.value == "system"


def test_message_role_is_str_enum():
    # StrEnum members compare equal to their string value.
    assert MessageRole.USER == "user"
    assert "user" == MessageRole.USER

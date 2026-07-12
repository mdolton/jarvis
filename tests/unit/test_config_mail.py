"""MailConfig / JarvisConfig validators: the Mailtrap sandbox-vs-sending
distinction must be enforced at startup, loudly, because the wrong one
returns success on every send while delivering nothing."""

import pytest
from pydantic import ValidationError

from jarvis.config.schema import AuthConfig, JarvisConfig, LLMConfig, MailConfig

_LLM = LLMConfig(base_url="http://localhost:1234/v1", api_key="x", model="m")


def test_mail_defaults_to_console():
    cfg = MailConfig()
    assert cfg.provider == "console"
    assert cfg.sandbox is False


def test_mailtrap_api_requires_token_and_refuses_sandbox():
    with pytest.raises(ValidationError, match="api_token"):
        MailConfig(provider="mailtrap_api")
    with pytest.raises(ValidationError, match="sandbox"):
        MailConfig(provider="mailtrap_api", api_token="tok", sandbox=True)


def test_smtp_sandbox_host_must_be_declared():
    # Pointing at the sandbox without saying so is the silent-capture trap.
    with pytest.raises(ValidationError, match="SANDBOX"):
        MailConfig(provider="mailtrap_smtp", smtp_host="sandbox.smtp.mailtrap.io", api_token="t")
    # Declared sandbox on a sandbox host is fine (local dev).
    ok = MailConfig(
        provider="mailtrap_smtp",
        smtp_host="sandbox.smtp.mailtrap.io",
        sandbox=True,
        smtp_username="inbox-user",
        smtp_password="inbox-pass",
    )
    assert ok.sandbox is True
    # The reverse lie — sandbox: true on a live host — is also rejected.
    with pytest.raises(ValidationError, match="must agree"):
        MailConfig(
            provider="mailtrap_smtp",
            smtp_host="live.smtp.mailtrap.io",
            sandbox=True,
            api_token="t",
        )


def test_smtp_requires_host_and_credentials():
    with pytest.raises(ValidationError, match="smtp_host"):
        MailConfig(provider="mailtrap_smtp")
    with pytest.raises(ValidationError, match="smtp_password"):
        MailConfig(provider="mailtrap_smtp", smtp_host="live.smtp.mailtrap.io")


def test_auth_enabled_refuses_sandbox_mail():
    sandbox_mail = MailConfig(
        provider="mailtrap_smtp",
        smtp_host="sandbox.smtp.mailtrap.io",
        sandbox=True,
        smtp_password="inbox-pass",
    )
    # Fine while auth is off (local dev), fatal the moment auth is on:
    # production wired to the sandbox looks healthy and delivers nothing.
    JarvisConfig(llm=_LLM, mail=sandbox_mail)
    with pytest.raises(ValidationError, match="sandbox"):
        JarvisConfig(llm=_LLM, auth=AuthConfig(enabled=True), mail=sandbox_mail)


def test_forwarded_allow_ips_rejects_wildcard():
    JarvisConfig(llm=_LLM, forwarded_allow_ips="172.18.0.2")  # proxy IP: fine
    with pytest.raises(ValidationError, match="never"):
        JarvisConfig(llm=_LLM, forwarded_allow_ips="*")

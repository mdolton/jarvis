import httpx
import pytest

from jarvis.auth.mailer import (
    MAILTRAP_SEND_URL,
    ConsoleMailer,
    MailtrapApiMailer,
    MailtrapSmtpMailer,
    build_mailer,
)
from jarvis.config.schema import MailConfig


async def test_console_mailer_logs_the_code(caplog):
    with caplog.at_level("INFO", logger="jarvis.auth.mailer"):
        await ConsoleMailer().send(
            to="me@example.com", subject="123456 is your code", text="123456"
        )
    assert "123456" in caplog.text
    assert "me@example.com" in caplog.text


async def test_mailtrap_api_mailer_posts_send_endpoint_with_bearer_token():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = request.read()
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mailer = MailtrapApiMailer(api_token="tok-123", from_addr="jarvis@example.com", http=http)
        await mailer.send(to="me@example.com", subject="hi", text="body")

    assert seen["url"] == MAILTRAP_SEND_URL
    # Verified against docs.mailtrap.io/developers/authentication: the send
    # API accepts `Authorization: Bearer <token>` (or `Api-Token`).
    assert seen["auth"] == "Bearer tok-123"
    assert b'"me@example.com"' in seen["json"]
    assert b'"jarvis@example.com"' in seen["json"]


async def test_mailtrap_api_mailer_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": ["Unauthorized"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mailer = MailtrapApiMailer(api_token="bad", from_addr="jarvis@example.com", http=http)
        with pytest.raises(httpx.HTTPStatusError):
            await mailer.send(to="me@example.com", subject="hi", text="body")


def test_build_mailer_selects_provider():
    assert isinstance(build_mailer(MailConfig()), ConsoleMailer)
    assert isinstance(
        build_mailer(MailConfig(provider="mailtrap_api", api_token="tok")),
        MailtrapApiMailer,
    )
    smtp = build_mailer(
        MailConfig(
            provider="mailtrap_smtp",
            smtp_host="live.smtp.mailtrap.io",
            api_token="tok",
        )
    )
    assert isinstance(smtp, MailtrapSmtpMailer)
    # Live SMTP defaults: user "api", password = the API token.
    assert smtp._username == "api"
    assert smtp._password == "tok"

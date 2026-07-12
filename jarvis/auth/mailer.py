"""Outbound mail for login codes: Mailtrap (API or SMTP) or the console.

Mailtrap is TWO products (see MailConfig): Email Sending delivers real mail;
the Email Sandbox captures it and never delivers — while reporting success.
Config validation keeps them distinct and refuses to start production against
the sandbox; this module just sends to whatever the validated config says.

The Mailtrap HTTP API is preferred over SMTP: smtplib is blocking, and this
is a single-process app — a stalled send would freeze Discord and the
scheduler with it. The SMTP path exists for the sandbox (which is SMTP-only)
and runs in a worker thread for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

from jarvis.config.schema import MailConfig

logger = logging.getLogger(__name__)

MAILTRAP_SEND_URL = "https://send.api.mailtrap.io/api/send"


class Mailer(Protocol):
    async def send(self, *, to: str, subject: str, text: str) -> None: ...


class ConsoleMailer:
    """Logs the mail instead of sending it — local dev and hermetic tests."""

    async def send(self, *, to: str, subject: str, text: str) -> None:
        logger.info("console mailer: to=%s subject=%r\n%s", to, subject, text)


class MailtrapApiMailer:
    """POST to the Mailtrap Email Sending API (the real-delivery product).

    Auth header verified against https://docs.mailtrap.io/developers/authentication:
    the API accepts `Api-Token: <token>` or `Authorization: Bearer <token>`.
    """

    def __init__(
        self,
        *,
        api_token: str,
        from_addr: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_token = api_token
        self._from_addr = from_addr
        self._http = http

    async def send(self, *, to: str, subject: str, text: str) -> None:
        payload = {
            "from": {"email": self._from_addr, "name": "Jarvis"},
            "to": [{"email": to}],
            "subject": subject,
            "text": text,
            "category": "auth",
        }
        headers = {"Authorization": f"Bearer {self._api_token}"}
        if self._http is not None:
            response = await self._http.post(MAILTRAP_SEND_URL, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(MAILTRAP_SEND_URL, json=payload, headers=headers)
        response.raise_for_status()


class MailtrapSmtpMailer:
    """SMTP path: live.smtp.mailtrap.io delivers; sandbox.smtp.mailtrap.io
    only captures. smtplib is blocking, so the send runs in a worker thread.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_addr = from_addr

    async def send(self, *, to: str, subject: str, text: str) -> None:
        await asyncio.to_thread(self._send_sync, to, subject, text)

    def _send_sync(self, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = self._from_addr
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(self._username, self._password)
            smtp.send_message(message)


def build_mailer(config: MailConfig) -> Mailer:
    """Construct the configured mailer. Config validation has already refused
    the silent-failure combinations (sandbox in production, missing tokens)."""
    if config.provider == "mailtrap_api":
        assert config.api_token is not None  # enforced by MailConfig validation
        return MailtrapApiMailer(api_token=config.api_token, from_addr=config.from_addr)
    if config.provider == "mailtrap_smtp":
        assert config.smtp_host is not None  # enforced by MailConfig validation
        # Live SMTP authenticates as user "api" with the API token as the
        # password; sandbox inboxes carry their own credentials.
        username = config.smtp_username or "api"
        password = config.smtp_password or config.api_token or ""
        return MailtrapSmtpMailer(
            host=config.smtp_host,
            port=config.smtp_port,
            username=username,
            password=password,
            from_addr=config.from_addr,
        )
    return ConsoleMailer()

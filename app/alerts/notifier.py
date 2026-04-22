from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.config.settings import AlertSettings

logger = logging.getLogger(__name__)


class AlertNotifier:
    def __init__(self, settings: AlertSettings) -> None:
        self.settings = settings

    async def send(self, title: str, body: str) -> None:
        if self.settings.telegram_enabled:
            await self._telegram(title, body)
        if self.settings.email_enabled:
            self._email(title, body)
        logger.warning("alert", extra={"title": title, "body": body})

    async def _telegram(self, title: str, body: str) -> None:
        if not (self.settings.telegram_bot_token and self.settings.telegram_chat_id):
            return
        token = self.settings.telegram_bot_token.get_secret_value()
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": self.settings.telegram_chat_id, "text": f"{title}\n{body}"},
            )

    def _email(self, title: str, body: str) -> None:
        if not (self.settings.smtp_host and self.settings.email_from and self.settings.email_to):
            return
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = self.settings.email_from
        message["To"] = ", ".join(self.settings.email_to)
        message.set_content(body)
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as smtp:
            smtp.starttls()
            if self.settings.smtp_username and self.settings.smtp_password:
                smtp.login(
                    self.settings.smtp_username, self.settings.smtp_password.get_secret_value()
                )
            smtp.send_message(message)

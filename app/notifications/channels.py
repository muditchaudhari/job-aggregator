"""Notification channels.

Each is a thin transport. Rendering lives in ``templates.py``, and the decision
about *whether* to send lives in ``dispatcher.py`` — a channel's only job is to
put an already-composed message on the wire and raise if it could not.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING

import httpx

from app.core.config import get_settings
from app.core.errors import NotificationError
from app.core.logging import get_logger
from app.models.enums import NotificationChannel
from app.notifications.base import NotificationPayload, NotificationSender
from app.notifications.templates import (
    render_email_html,
    render_email_subject,
    render_email_text,
    render_slack_blocks,
    render_telegram,
)

if TYPE_CHECKING:
    from app.models.user import User

logger = get_logger(__name__)

_HTTP_TIMEOUT = 15.0


class EmailSender(NotificationSender):
    channel = NotificationChannel.EMAIL

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.smtp_host and settings.notify_from_email)

    def send(self, user: User, payload: NotificationPayload) -> None:
        settings = get_settings()

        message = EmailMessage()
        message["Subject"] = render_email_subject(payload)
        message["From"] = settings.notify_from_email
        message["To"] = user.email
        # Plain text first, HTML second: ``set_content`` then ``add_alternative``
        # produces multipart/alternative in the order clients expect.
        message.set_content(render_email_text(payload))
        message.add_alternative(render_email_html(payload), subtype="html")

        try:
            with smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=_HTTP_TIMEOUT
            ) as smtp:
                if settings.smtp_use_tls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(
                "SMTP delivery failed", recipient=user.email, detail=str(exc)
            ) from exc


class SlackSender(NotificationSender):
    channel = NotificationChannel.SLACK

    def is_configured(self) -> bool:
        return bool(get_settings().slack_webhook_url)

    def send(self, user: User, payload: NotificationPayload) -> None:
        webhook = get_settings().slack_webhook_url
        if not webhook:
            raise NotificationError("SLACK_WEBHOOK_URL is not configured")

        try:
            response = httpx.post(
                webhook, json=render_slack_blocks(payload), timeout=_HTTP_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise NotificationError("Slack request failed", detail=str(exc)) from exc

        if response.status_code >= 400:
            raise NotificationError(
                "Slack rejected the message",
                status=response.status_code,
                body=response.text[:200],
            )


class TelegramSender(NotificationSender):
    channel = NotificationChannel.TELEGRAM

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    def send(self, user: User, payload: NotificationPayload) -> None:
        settings = get_settings()
        if not self.is_configured():
            raise NotificationError("Telegram bot token or chat id is not configured")

        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        try:
            response = httpx.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": render_telegram(payload),
                    "parse_mode": "HTML",
                    # The link preview would duplicate content already in the
                    # message and make the feed unreadable at volume.
                    "disable_web_page_preview": True,
                },
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise NotificationError("Telegram request failed", detail=str(exc)) from exc

        if response.status_code >= 400:
            raise NotificationError(
                "Telegram rejected the message",
                status=response.status_code,
                body=response.text[:200],
            )


class ResendSender(NotificationSender):
    """Resend's HTTP API.

    Preferred over SMTP for hosted runs: CI runners and many PaaS providers
    block outbound port 25/587 entirely, and an HTTPS POST is unaffected. It
    also removes the need to put a mail password in the environment — the API
    key is scoped to sending and nothing else.
    """

    channel = NotificationChannel.RESEND

    def is_configured(self) -> bool:
        return bool(get_settings().resend_api_key)

    def send(self, user: User, payload: NotificationPayload) -> None:
        settings = get_settings()
        if not settings.resend_api_key:
            raise NotificationError("RESEND_API_KEY is not configured")

        try:
            response = httpx.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.resend_from or settings.notify_from_email,
                    "to": [user.email],
                    "subject": render_email_subject(payload),
                    "html": render_email_html(payload),
                    "text": render_email_text(payload),
                },
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise NotificationError("Resend request failed", detail=str(exc)) from exc

        if response.status_code >= 400:
            # Resend explains refusals in the body — an unverified sender
            # domain being the usual one — so surface it rather than a bare
            # status code the user would have to go digging about.
            raise NotificationError(
                "Resend rejected the message",
                status=response.status_code,
                body=response.text[:300],
            )


_SENDERS: dict[NotificationChannel, type[NotificationSender]] = {
    NotificationChannel.EMAIL: EmailSender,
    NotificationChannel.RESEND: ResendSender,
    NotificationChannel.SLACK: SlackSender,
    NotificationChannel.TELEGRAM: TelegramSender,
}


def build_sender(channel: NotificationChannel) -> NotificationSender:
    sender_class = _SENDERS.get(channel)
    if sender_class is None:
        raise NotificationError(f"unsupported channel {channel!r}")
    return sender_class()

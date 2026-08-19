"""
Email Notifier

SMTP-based implementation of the Notifier interface. This is the only file
in the codebase that knows about SMTP/email message formatting - swapping in
or adding WhatsApp later means adding e.g. whatsapp_notifier.py with the same
Notifier interface, nothing here or in NotificationService needs to change.
"""

import logging
import smtplib
from email.mime.text import MIMEText

from .base import Notifier, NotifierError

logger = logging.getLogger(__name__)


class EmailNotifier(Notifier):
    """Sends notifications via SMTP email."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        to_email: str,
    ):
        if not all([smtp_host, smtp_user, smtp_password, to_email]):
            raise NotifierError(
                "EmailNotifier is missing required SMTP configuration "
                "(SMTP_HOST, SMTP_USER, SMTP_PASSWORD, NOTIFY_TO_EMAIL)."
            )
        self._host = smtp_host
        self._port = smtp_port
        self._user = smtp_user
        self._password = smtp_password
        self._to_email = to_email

    def send(self, subject: str, body: str) -> None:
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = self._user
        message["To"] = self._to_email

        try:
            with smtplib.SMTP(self._host, self._port, timeout=15) as server:
                server.starttls()
                server.login(self._user, self._password)
                server.sendmail(self._user, [self._to_email], message.as_string())
            logger.info("Email sent: '%s' -> %s", subject, self._to_email)
        except smtplib.SMTPException as exc:
            logger.error("SMTP error while sending email: %s", exc, exc_info=True)
            raise NotifierError(f"Failed to send email: {exc}") from exc
        except OSError as exc:
            # Covers connection/network failures (e.g. host unreachable, timeout)
            logger.error("Network error while sending email: %s", exc, exc_info=True)
            raise NotifierError(f"Network error sending email: {exc}") from exc

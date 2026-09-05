"""
Notifier Interface

Generic contract for any notification channel. NotificationService only ever
talks to this interface - it never knows whether it's email, WhatsApp, SMS,
or anything else. Adding a new channel means writing one new class here and
pointing config at it; no changes to NotificationService or the watcher.
"""

from abc import ABC, abstractmethod


class Notifier(ABC):
    """Abstract base class for a notification channel."""

    @abstractmethod
    def send(self, subject: str, body: str) -> None:
        """
        Send a notification.

        Args:
            subject: Short subject/title for the notification.
            body: Full message body.

        Raises:
            NotifierError: on any failure to deliver the notification.
        """
        raise NotImplementedError


class NotifierError(Exception):
    """Raised when a notifier fails to deliver a notification."""
    pass

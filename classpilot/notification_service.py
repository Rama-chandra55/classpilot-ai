"""
Notification Service

The single place that ties an event to "generate an AI message, then deliver
it". This is what keeps watcher.py and main.py's handle_event() lightweight:
they just call notify(event_type, assignment_name) and this service does the
LLM call + formatting + delivery + retry handling.

Design:
- The LLM is called exactly ONCE per real event.
- If sending fails, retry() reuses the already-generated message instead of
  calling the LLM again - we never spend a second LLM call on a delivery
  problem.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from .events import EventType
from .llm.base import LLMProvider, LLMProviderError
from .notifier.base import Notifier, NotifierError

logger = logging.getLogger(__name__)

_SIGNATURE = "\n\n— ClassPilot AI 🤖"

_SUBJECTS = {
    EventType.NEW_ASSIGNMENT: "📚 New Assignment: {assignment_name}",
    EventType.DEADLINE_REMINDER: "⏰ Deadline Reminder: {assignment_name}",
    EventType.SUBMISSION_SUCCESS: "✅ Submitted: {assignment_name}",
}


@dataclass
class PendingNotification:
    """Holds an already-generated message so retries don't re-call the LLM."""
    event_type: EventType
    assignment_name: str
    message: str


class NotificationService:
    """Generates an AI message for an event and delivers it via a Notifier."""

    def __init__(self, llm_provider: LLMProvider, notifier: Notifier):
        self._llm = llm_provider
        self._notifier = notifier

    def notify(self, event_type: EventType, assignment_name: str) -> bool:
        """
        Generate a fresh AI message for this event and send it.

        Returns:
            True if the notification was delivered, False otherwise.
        """
        try:
            message = self._llm.generate_message(event_type, assignment_name)
        except LLMProviderError as exc:
            logger.error(
                "Could not generate AI message for %s ('%s'): %s",
                event_type, assignment_name, exc,
            )
            return False

        pending = PendingNotification(
            event_type=event_type, assignment_name=assignment_name, message=message
        )
        return self._deliver(pending)

    def retry(self, pending: PendingNotification) -> bool:
        """
        Re-attempt delivery of an already-generated message, without calling
        the LLM again.
        """
        logger.info(
            "Retrying delivery for %s ('%s') using previously generated message",
            pending.event_type, pending.assignment_name,
        )
        return self._deliver(pending)

    def _deliver(self, pending: PendingNotification) -> bool:
        subject_template = _SUBJECTS.get(pending.event_type, "{assignment_name}")
        subject = subject_template.format(assignment_name=pending.assignment_name)
        body = f"{pending.assignment_name}\n\n{pending.message}{_SIGNATURE}"

        try:
            self._notifier.send(subject, body)
            return True
        except NotifierError as exc:
            logger.error(
                "Failed to deliver notification for %s ('%s'): %s. "
                "Message preserved for retry: %r",
                pending.event_type, pending.assignment_name, exc, pending.message,
            )
            return False

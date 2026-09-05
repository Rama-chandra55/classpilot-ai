"""
Event Types

Single source of truth for the kinds of events ClassPilot AI can notify
about. Shared by the watcher (which detects them), the LLM layer (which
picks a prompt based on them), and the notification service (which routes
them to a notifier).
"""

from enum import Enum


class EventType(str, Enum):
    NEW_ASSIGNMENT = "NEW_ASSIGNMENT"
    DEADLINE_REMINDER = "DEADLINE_REMINDER"
    SUBMISSION_SUCCESS = "SUBMISSION_SUCCESS"

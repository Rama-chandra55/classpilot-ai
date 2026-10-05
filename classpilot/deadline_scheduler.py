"""
Feature 3: Deadline Reminders (Phase 5B Job Architecture)

For every assignment that has a due datetime, this evaluates deadlines synchronously
during the background job's execution (1 day / 6 hours / 1 hour / 15 minutes before).

1. It fetches all known assignments for the user.
2. Computes the offset between current time and the deadline.
3. If an offset boundary was crossed, it attempts an atomic claim in StateStore.
4. If the claim succeeds, it calls NotificationService.notify().

No APScheduler threads or in-memory state are used.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from .config import ClassPilotConfig
from .events import EventType
from .notification_service import NotificationService
from .state_store import StateStore, DEFAULT_USER_ID

logger = logging.getLogger(__name__)


def _parse_due_datetime(due_date: dict | None, due_time: dict | None) -> datetime | None:
    """
    Convert Classroom API due_date + due_time dicts into a UTC datetime.
    Returns None if either component is missing (no deadline set).
    """
    if not due_date:
        return None
    try:
        year  = due_date["year"]
        month = due_date["month"]
        day   = due_date["day"]
        hour  = (due_time or {}).get("hours", 23)
        minute = (due_time or {}).get("minutes", 59)
        second = (due_time or {}).get("seconds", 0)
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Could not parse due datetime: %s", exc)
        return None


class DeadlineScheduler:
    """
    Evaluates and fires timed reminder jobs for one user's assignment deadlines.
    """

    def __init__(
        self,
        state_store: StateStore,
        notification_service: NotificationService,
        config: ClassPilotConfig,
        user_id: str = DEFAULT_USER_ID,
    ):
        self._state = state_store
        self._svc = notification_service
        self._offsets: tuple = config.deadline_alert_offsets_minutes
        self._user_id = user_id

    def evaluate_all(self) -> None:
        """
        Evaluate all known assignments for this user and fire any due reminders.
        Called on every poll cycle.
        """
        assignments = self._state.get_all_assignments(self._user_id)
        now = datetime.now(tz=timezone.utc)
        
        for assignment in assignments:
            due_date = json.loads(assignment["due_date_json"]) if assignment["due_date_json"] else None
            due_time = json.loads(assignment["due_time_json"]) if assignment["due_time_json"] else None
            
            due_dt = _parse_due_datetime(due_date, due_time)
            if due_dt is None:
                continue

            for offset_minutes in self._offsets:
                fire_at = due_dt - timedelta(minutes=offset_minutes)
                
                # If we haven't reached the fire time yet, don't send
                if fire_at > now:
                    continue
                
                # If the deadline has already passed, we shouldn't spam missed reminders
                # (unless it just recently passed, but we'll assume if due_dt <= now we skip entirely)
                # Actually, if we are within the offset, we send it. 
                # To prevent sending all past reminders when a watcher is first enabled:
                # We only send if the deadline is still in the future!
                if due_dt <= now:
                    continue

                self._fire_reminder(
                    assignment["course_id"],
                    assignment["assignment_id"],
                    assignment["title"],
                    offset_minutes,
                )

    def _fire_reminder(
        self,
        course_id: str,
        assignment_id: str,
        title: str,
        offset_minutes: int,
    ) -> None:
        """
        Guards against duplicate delivery with an atomic StateStore claim.
        """
        # Atomic claim!
        won = self._state.mark_reminder_sent(course_id, assignment_id, offset_minutes, user_id=self._user_id)
        if not won:
            return

        label = self._offset_label(offset_minutes)
        logger.info("Firing %s reminder for '%s' (user_id=%s)", label, title, self._user_id)

        delivered = self._svc.notify(EventType.DEADLINE_REMINDER, title)

        if delivered:
            logger.info("Reminder delivered: '%s' (%s)", title, label)
        else:
            logger.error(
                "Reminder delivery failed for '%s' (%s) — will NOT retry automatically",
                title, label,
            )

    def _offset_label(self, offset_minutes: int) -> str:
        if offset_minutes >= 60:
            hours = offset_minutes // 60
            return f"{hours}h"
        return f"{offset_minutes}min"

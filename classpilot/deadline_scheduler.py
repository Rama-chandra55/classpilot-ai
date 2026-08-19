"""
Feature 3: Deadline Scheduler

For every assignment that has a due datetime, this schedules up to four
one-shot APScheduler jobs (1 day / 6 hours / 1 hour / 15 minutes before
the deadline). Each job:
  1. Checks StateStore so it never fires twice (survives restarts).
  2. Calls NotificationService.notify(DEADLINE_REMINDER, title).
  3. Marks itself sent in StateStore.

On a deadline change the old jobs are removed and new ones are registered,
so the student always gets reminders relative to the *current* due time.

Google Classroom API note:
  due_date  → {"year": int, "month": int, "day": int}   (civil date)
  due_time  → {"hours": int, "minutes": int, ...}        (UTC time-of-day)
  Together they form a UTC datetime.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import ClassPilotConfig
from .events import EventType
from .notification_service import NotificationService
from .state_store import StateStore
from .watcher import AssignmentEvent

logger = logging.getLogger(__name__)


def _parse_due_datetime(
    due_date: Optional[dict], due_time: Optional[dict]
) -> Optional[datetime]:
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
    Manages timed reminder jobs for assignment deadlines.

    Usage in main.py:
        ds = DeadlineScheduler(state_store, notification_service, config)
        # ... build watcher and APScheduler ...
        ds.attach_scheduler(apscheduler)
        apscheduler.start()
    """

    def __init__(
        self,
        state_store: StateStore,
        notification_service: NotificationService,
        config: ClassPilotConfig,
    ):
        self._state = state_store
        self._svc = notification_service
        self._offsets: tuple = config.deadline_alert_offsets_minutes
        self._scheduler = None  # set via attach_scheduler() before start()

    def attach_scheduler(self, scheduler) -> None:
        """Wire the APScheduler instance after it has been built."""
        self._scheduler = scheduler
        logger.debug("DeadlineScheduler attached to APScheduler")

    # ------------------------------------------------------------------
    # Public API called from handle_event()
    # ------------------------------------------------------------------

    def schedule(self, event: AssignmentEvent) -> None:
        """Register reminder jobs for a newly detected assignment."""
        self._register_jobs(event)

    def reschedule(self, event: AssignmentEvent) -> None:
        """
        Cancel any existing reminder jobs for this assignment and register
        fresh ones against the updated deadline.
        """
        self._cancel_jobs(event.course_id, event.assignment_id)
        self._register_jobs(event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_jobs(self, event: AssignmentEvent) -> None:
        if self._scheduler is None:
            logger.error(
                "DeadlineScheduler.attach_scheduler() was never called — "
                "cannot schedule reminders for '%s'", event.title
            )
            return

        due_dt = _parse_due_datetime(event.due_date, event.due_time)
        if due_dt is None:
            logger.info(
                "No due datetime for '%s' — skipping reminder scheduling", event.title
            )
            return

        now = datetime.now(tz=timezone.utc)
        scheduled_count = 0

        for offset_minutes in self._offsets:
            fire_at = due_dt - timedelta(minutes=offset_minutes)

            if fire_at <= now:
                logger.debug(
                    "Skipping %d-min reminder for '%s' — fire time is in the past",
                    offset_minutes, event.title,
                )
                continue

            if self._state.has_sent_reminder(
                event.course_id, event.assignment_id, offset_minutes
            ):
                logger.debug(
                    "Skipping %d-min reminder for '%s' — already sent",
                    offset_minutes, event.title,
                )
                continue

            job_id = self._job_id(event.course_id, event.assignment_id, offset_minutes)

            try:
                self._scheduler.add_job(
                    _fire_reminder,
                    trigger="date",
                    run_date=fire_at,
                    args=[
                        event.course_id,
                        event.assignment_id,
                        event.title,
                        offset_minutes,
                        self._state,
                        self._svc,
                    ],
                    id=job_id,
                    replace_existing=True,
                )
                logger.info(
                    "Reminder scheduled: '%s' at %s UTC (%d min before deadline)",
                    event.title, fire_at.strftime("%Y-%m-%d %H:%M"), offset_minutes,
                )
                scheduled_count += 1
            except Exception:
                logger.error(
                    "Failed to schedule %d-min reminder for '%s'",
                    offset_minutes, event.title, exc_info=True,
                )

        logger.info(
            "Scheduled %d reminder job(s) for '%s'", scheduled_count, event.title
        )

    def _cancel_jobs(self, course_id: str, assignment_id: str) -> None:
        if self._scheduler is None:
            return
        for offset_minutes in self._offsets:
            job_id = self._job_id(course_id, assignment_id, offset_minutes)
            try:
                self._scheduler.remove_job(job_id)
                logger.info("Cancelled reminder job: %s", job_id)
            except Exception:
                # Job may not exist (already fired, or never scheduled) — fine
                pass

    @staticmethod
    def _job_id(course_id: str, assignment_id: str, offset_minutes: int) -> str:
        return f"reminder_{course_id}_{assignment_id}_{offset_minutes}"


# ------------------------------------------------------------------
# Top-level function executed by APScheduler in a thread pool
# (must be module-level, not a lambda, for APScheduler serialisation)
# ------------------------------------------------------------------

def _fire_reminder(
    course_id: str,
    assignment_id: str,
    title: str,
    offset_minutes: int,
    state: StateStore,
    svc: NotificationService,
) -> None:
    """
    Called by APScheduler at the scheduled fire time.
    Guards against duplicate delivery with a StateStore check.
    """
    if state.has_sent_reminder(course_id, assignment_id, offset_minutes):
        logger.info(
            "Reminder already sent for '%s' at offset %d min — skipping",
            title, offset_minutes,
        )
        return

    label = _offset_label(offset_minutes)
    logger.info("Firing %s reminder for '%s'", label, title)

    delivered = svc.notify(EventType.DEADLINE_REMINDER, title)

    if delivered:
        state.mark_reminder_sent(course_id, assignment_id, offset_minutes)
        logger.info("Reminder delivered and recorded: '%s' (%s)", title, label)
    else:
        logger.error(
            "Reminder delivery failed for '%s' (%s) — will NOT retry automatically",
            title, label,
        )


def _offset_label(offset_minutes: int) -> str:
    if offset_minutes >= 60:
        hours = offset_minutes // 60
        return f"{hours}h"
    return f"{offset_minutes}min"

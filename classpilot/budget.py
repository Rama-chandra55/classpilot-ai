"""
ClassPilot AI — Global Notification Budget & Rate Limiting

Enforces a global cap (MAX_NOTIFICATIONS_PER_RUN) across all users, courses,
new assignments, deadline updates, and reminders within a single production execution.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class NotificationBudget:
    """
    Thread-safe tracker for global notifications allowed within a single execution run.
    Enforces MAX_NOTIFICATIONS_PER_RUN across all accounts, courses, assignment
    notifications, deadline updates, and reminders.
    Tracks attempted, successful, failed, deferred, and suppressed notification counts.
    """

    def __init__(self, limit: int = 5):
        self.limit = max(0, int(limit))
        self.attempted_count = 0
        self.successful_count = 0
        self.failed_count = 0
        self.deferred_count = 0
        self.suppressed_count = 0
        self._lock = threading.Lock()

    def can_send(self) -> bool:
        """Return True if under the limit, False if exhausted."""
        with self._lock:
            return self.attempted_count < self.limit

    def record_attempt(self) -> bool:
        """
        Atomically check and record an attempt if within limit.
        Returns True if attempt is allowed, False if cap was reached.
        """
        with self._lock:
            if self.attempted_count < self.limit:
                self.attempted_count += 1
                return True
            return False

    def record_success(self) -> None:
        """Record successful notification delivery."""
        with self._lock:
            self.successful_count += 1

    def record_failure(self) -> None:
        """Record failed notification delivery (eligible for retry)."""
        with self._lock:
            self.failed_count += 1

    def record_deferred(self) -> None:
        """Record notification deferred due to budget cap."""
        with self._lock:
            self.deferred_count += 1

    def record_suppressed(self) -> None:
        """Record notification suppressed by deduplication or historical baseline."""
        with self._lock:
            self.suppressed_count += 1

    # Aliases for backward compatibility
    def record_send_success(self) -> None:
        self.record_success()

    def record_skipped(self) -> None:
        self.record_deferred()

    @property
    def sent_count(self) -> int:
        with self._lock:
            return self.successful_count

    @property
    def skipped_count(self) -> int:
        with self._lock:
            return self.deferred_count

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self.attempted_count)

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {
                "limit": self.limit,
                "attempted": self.attempted_count,
                "successful": self.successful_count,
                "failed": self.failed_count,
                "deferred": self.deferred_count,
                "suppressed": self.suppressed_count,
                "remaining": max(0, self.limit - self.attempted_count),
                # Aliases for existing callers/tests
                "sent": self.successful_count,
                "skipped": self.deferred_count,
            }

    def __str__(self) -> str:
        s = self.summary()
        return (
            f"Notification Budget: {s['successful']} successful, "
            f"{s['failed']} failed, {s['deferred']} deferred, "
            f"{s['suppressed']} suppressed, {s['attempted']} attempted "
            f"(limit={s['limit']}, remaining={s['remaining']})"
        )

"""
ClassPilot AI - Standalone Watcher Entrypoint

Runs the background watcher + deadline reminders as a long-running process.
Use this when you want ClassPilot to run independently (cron, systemd, screen).

For MCP server mode (Claude Desktop, Cursor, ChatGPT):
    python -m classpilot.server
    or: classpilot-ai-mcp
"""

import logging

from classpilot.services import build_services

logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Starting ClassPilot AI (standalone watcher mode)...")
    services = build_services()
    services.start_background_scheduler()
    services.run_forever()


if __name__ == "__main__":
    main()


# ── Backward-compatible re-export used by tests ───────────────────────────────
# Tests import build_handle_event from main; keep this shim so they don't break.

from classpilot.notification_service import NotificationService
from classpilot.deadline_scheduler import DeadlineScheduler
from classpilot.watcher import AssignmentEvent
from classpilot.events import EventType


def build_handle_event(notification_service: NotificationService,
                       deadline_scheduler: DeadlineScheduler):
    """Shim retained for test compatibility. Logic lives in AppServices._handle_event."""
    def handle_event(event: AssignmentEvent) -> None:
        if event.kind == "new":
            notification_service.notify(EventType.NEW_ASSIGNMENT, event.title)
            deadline_scheduler.schedule(event)
        elif event.kind == "deadline_updated":
            deadline_scheduler.reschedule(event)
    return handle_event

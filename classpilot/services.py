"""
AppServices

Single container that builds and owns every shared service instance.
Both main.py (standalone watcher mode) and server.py (MCP mode) import
this — zero duplication of wiring logic.

Ownership:
  StateStore           → SQLite dedup
  NotificationService  → LLM + notifier
  DeadlineScheduler    → timed reminder jobs
  AssignmentWatcher    → Classroom poll + event dispatch
"""

import logging
import threading
import time

from .config import ClassPilotConfig, get_config
from .deadline_scheduler import DeadlineScheduler
from .events import EventType
from .llm import create_llm_provider
from .logging_setup import configure_logging
from .notification_service import NotificationService
from .notifier import create_notifier
from .scheduler import build_scheduler
from .state_store import StateStore
from .watcher import AssignmentEvent, AssignmentWatcher

logger = logging.getLogger(__name__)


class AppServices:
    """
    Builds and holds every ClassPilot AI service instance.

    Usage:
        services = AppServices(config)
        services.start_background_scheduler()   # non-blocking
        # ... main thread does other work (MCP server, blocking wait, etc.)
        services.stop_scheduler()
    """

    def __init__(self, config: ClassPilotConfig):
        self.config = config

        self.state_store = StateStore(config.state_db_path)

        llm_provider = create_llm_provider(config)
        notifier = create_notifier(config)
        self.notification_service = NotificationService(llm_provider, notifier)

        self.deadline_scheduler = DeadlineScheduler(
            self.state_store, self.notification_service, config
        )

        self.watcher = AssignmentWatcher(
            state_store=self.state_store,
            on_event=self._handle_event,
        )

        self._apscheduler = None
        self._scheduler_lock = threading.Lock()

    # ── Event dispatch (identical logic to the old main.py handle_event) ──────

    def _handle_event(self, event: AssignmentEvent) -> None:
        if event.kind == "new":
            self.notification_service.notify(EventType.NEW_ASSIGNMENT, event.title)
            self.deadline_scheduler.schedule(event)
        elif event.kind == "deadline_updated":
            self.deadline_scheduler.reschedule(event)

    # ── Scheduler lifecycle ───────────────────────────────────────────────────

    def start_background_scheduler(self) -> None:
        """Start the background watcher + deadline scheduler (non-blocking)."""
        with self._scheduler_lock:
            if self._apscheduler is not None and self._apscheduler.running:
                logger.info("Scheduler is already running.")
                return
            self._apscheduler = build_scheduler(self.config, self.watcher)
            self.deadline_scheduler.attach_scheduler(self._apscheduler)
            self._apscheduler.start()
            logger.info("Background scheduler started.")

    def stop_scheduler(self) -> None:
        """Gracefully stop the background scheduler."""
        with self._scheduler_lock:
            if self._apscheduler and self._apscheduler.running:
                self._apscheduler.shutdown(wait=False)
                logger.info("Background scheduler stopped.")
            else:
                logger.info("Scheduler was not running.")

    @property
    def is_watcher_running(self) -> bool:
        return bool(self._apscheduler and self._apscheduler.running)

    # ── Convenience: block main thread (standalone mode only) ─────────────────

    def run_forever(self) -> None:
        """
        Block the calling thread until KeyboardInterrupt / SystemExit.
        Used by main.py standalone watcher mode only.
        The MCP server (server.py) blocks on mcp.run() instead.
        """
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logger.info("ClassPilot AI shutting down.")
            self.stop_scheduler()


def build_services(validate: bool = True) -> AppServices:
    """
    Convenience factory used by both main.py and server.py.
    Loads config, configures logging, validates, returns AppServices.
    """
    config = get_config()
    configure_logging(config.log_level)
    if validate:
        try:
            config.validate()
        except ValueError as exc:
            logger.critical("Configuration error: %s", exc)
            raise SystemExit(1) from exc
    return AppServices(config)

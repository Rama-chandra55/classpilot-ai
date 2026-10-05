"""
AppServices

Single container that builds and owns every shared service instance.
Both main.py (standalone watcher mode) and server.py (MCP mode) import
this — zero duplication of wiring logic.

Ownership:
  StateStore           → Postgres dedup
  NotificationService  → LLM + notifier
  DeadlineScheduler    → timed reminder evaluation
  AssignmentWatcher    → Classroom poll + event dispatch

Phase 5B Job Architecture:
  AppServices no longer manages an APScheduler thread. It is simply a
  dependency container for the background job or MCP server.
"""

import logging
from typing import Optional

from .config import ClassPilotConfig, get_config
from .deadline_scheduler import DeadlineScheduler
from .events import EventType
from .llm import create_llm_provider
from .logging_setup import configure_logging
from .notification_service import NotificationService
from .notifier import create_notifier
from .state_store import StateStore, DEFAULT_USER_ID
from .watcher import AssignmentEvent, AssignmentWatcher

logger = logging.getLogger(__name__)


class AppServices:
    """
    Builds and holds every ClassPilot AI service instance for ONE user.
    """

    def __init__(
        self,
        config: ClassPilotConfig,
        user_id: str,
        credentials,
        to_email: Optional[str] = None,
    ):
        self.config = config
        self.user_id = user_id
        self.credentials = credentials

        self.state_store = StateStore()

        llm_provider = create_llm_provider(config)
        notifier = create_notifier(config, to_email_override=to_email)
        self.notification_service = NotificationService(llm_provider, notifier)

        self.deadline_scheduler = DeadlineScheduler(
            self.state_store, self.notification_service, config, user_id=user_id,
        )

        self.watcher = AssignmentWatcher(
            state_store=self.state_store,
            on_event=self._handle_event,
            credentials=self.credentials,
            user_id=self.user_id,
        )

    # ── Event dispatch ────────────────────────────────────────────────────────

    def _handle_event(self, event: AssignmentEvent) -> None:
        if event.kind == "new":
            self.notification_service.notify(EventType.NEW_ASSIGNMENT, event.title)
        elif event.kind == "deadline_updated":
            self.notification_service.notify(EventType.DEADLINE_UPDATED, event.title)

    # ── Job Execution ─────────────────────────────────────────────────────────
    
    def run_poll_cycle(self) -> None:
        """Run one synchronous poll cycle (used by the background job)."""
        self.watcher.check_once()
        self.deadline_scheduler.evaluate_all()

    # ── Watcher config management ─────────────────────────────────────────────
    
    def enable_watcher(self) -> None:
        self.state_store.set_watcher_enabled(self.user_id, True)

    def disable_watcher(self) -> None:
        self.state_store.set_watcher_enabled(self.user_id, False)

    @property
    def is_watcher_running(self) -> bool:
        return self.state_store.get_watcher_enabled(self.user_id)


def build_services(validate: bool = True) -> AppServices:
    """
    LEGACY / explicit standalone entrypoint — used only by main.py.
    """
    config = get_config()
    configure_logging(config.log_level)
    if validate:
        try:
            config.validate()
        except ValueError as exc:
            logger.critical("Configuration error: %s", exc)
            raise SystemExit(1) from exc

    from classroom_suite_mcp.auth import get_auth
    credentials = get_auth().credentials
    return AppServices(config, user_id=DEFAULT_USER_ID, credentials=credentials)


def build_user_services(user_id: str, credentials, to_email: Optional[str] = None) -> AppServices:
    """
    Phase 3 identified-user entrypoint — used by server.py and job.py.
    """
    config = get_config()
    return AppServices(config, user_id=user_id, credentials=credentials, to_email=to_email)

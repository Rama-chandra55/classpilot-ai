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

Phase 3 — user-scoped, not a process-global singleton:
  AppServices now belongs to exactly one user: constructed with that
  user's `credentials` and `user_id`, its watcher/scheduler only ever act
  on that user's behalf (see watcher.py/deadline_scheduler.py). server.py
  keeps one AppServices per identified user (a dict keyed by user_id), so
  a user can never start, stop, or otherwise control another user's
  watcher — there is no longer a single shared instance to collide over.

  build_services() (no user_id/credentials) remains the LEGACY,
  explicitly-named standalone entrypoint used only by main.py: it
  resolves credentials from the vendor's token.json-backed global
  singleton, exactly as before Phase 3. build_user_services() is the new,
  explicitly-named entrypoint used by server.py's identified-user path —
  it always requires real credentials and never reaches for token.json.
  Naming them differently (rather than overloading one function with an
  optional parameter) makes accidentally calling the wrong one for the
  wrong context loud at the call site.
"""

import logging
import threading
import time
from typing import Optional

from .config import ClassPilotConfig, get_config
from .deadline_scheduler import DeadlineScheduler
from .events import EventType
from .llm import create_llm_provider
from .logging_setup import configure_logging
from .notification_service import NotificationService
from .notifier import create_notifier
from .scheduler import build_scheduler
from .state_store import StateStore, DEFAULT_USER_ID
from .watcher import AssignmentEvent, AssignmentWatcher

logger = logging.getLogger(__name__)


class AppServices:
    """
    Builds and holds every ClassPilot AI service instance for ONE user.

    Usage:
        services = AppServices(config, user_id=..., credentials=...)
        services.start_background_scheduler()   # non-blocking
        # ... main thread does other work (MCP server, blocking wait, etc.)
        services.stop_scheduler()
    """

    def __init__(
        self,
        config: ClassPilotConfig,
        user_id: str,
        credentials,
        to_email: Optional[str] = None,
    ):
        """
        Args:
            config: process-wide settings (deployment config, not per-user).
            user_id: whose Classroom/dedup-state this instance acts on.
            credentials: google.oauth2.credentials.Credentials for that
                         user — required, never defaulted or resolved
                         from a global singleton inside this class.
            to_email: where this user's watcher notifications are sent.
                      If None, falls back to config.notify_to_email (the
                      legacy single global recipient) — intended only for
                      the legacy standalone path (build_services()) where
                      there's no per-user email on file to use instead.
        """
        self.config = config
        self.user_id = user_id
        self.credentials = credentials

        self.state_store = StateStore(config.state_db_path)

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
                logger.info("Scheduler is already running for user_id=%s.", self.user_id)
                return
            self._apscheduler = build_scheduler(self.config, self.watcher)
            self.deadline_scheduler.attach_scheduler(self._apscheduler)
            self._apscheduler.start()
            logger.info("Background scheduler started for user_id=%s.", self.user_id)

    def stop_scheduler(self) -> None:
        """Gracefully stop the background scheduler."""
        with self._scheduler_lock:
            if self._apscheduler and self._apscheduler.running:
                self._apscheduler.shutdown(wait=False)
                logger.info("Background scheduler stopped for user_id=%s.", self.user_id)
            else:
                logger.info("Scheduler was not running for user_id=%s.", self.user_id)

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
    LEGACY / explicit standalone entrypoint — used only by main.py.

    Resolves credentials from the vendor's token.json-backed global
    singleton (classroom_suite_mcp.auth.get_auth()), exactly as ClassPilot
    behaved before Phase 3's multi-user work. Never called from the MCP
    tool path (server.py) — an identified PostgreSQL user must never reach
    this function; see build_user_services() for that path.
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
    Phase 3 identified-user entrypoint — used by server.py's
    start_assignment_watcher/stop_assignment_watcher tools.

    `credentials` must already be resolved (via
    classpilot.google_oauth.get_authorized_credentials) for the calling
    identity before this is called — this function never resolves or
    defaults credentials itself, so there is no path through it that
    could silently fall back to token.json.
    """
    config = get_config()
    return AppServices(config, user_id=user_id, credentials=credentials, to_email=to_email)

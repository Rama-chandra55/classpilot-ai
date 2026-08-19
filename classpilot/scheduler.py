"""
Scheduler

Drives the AssignmentWatcher on a fixed interval using APScheduler.
Uses BackgroundScheduler so it runs in a daemon thread, leaving the
main thread free for the MCP server (server.py) or a blocking wait
(main.py standalone mode).
"""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

from .config import ClassPilotConfig
from .watcher import AssignmentWatcher

logger = logging.getLogger(__name__)


def build_scheduler(config: ClassPilotConfig, watcher: AssignmentWatcher) -> BackgroundScheduler:
    """
    Construct (but do not start) the scheduler with the watcher job attached.
    Returns a BackgroundScheduler — call .start() to begin, .shutdown() to stop.
    """
    scheduler = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=4)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )

    scheduler.add_job(
        _run_watcher_safely,
        trigger="interval",
        minutes=config.watch_interval_minutes,
        args=[watcher],
        id="assignment_watcher",
        next_run_time=datetime.now(),  # run once immediately on startup
    )

    logger.info(
        "Scheduler configured: watcher runs every %d minute(s)",
        config.watch_interval_minutes,
    )
    return scheduler


def _run_watcher_safely(watcher: AssignmentWatcher) -> None:
    """Wraps watcher.check_once() so one bad poll never kills the scheduler."""
    try:
        watcher.check_once()
    except Exception:
        logger.error("Unhandled error during scheduled watcher run", exc_info=True)

"""
Logging setup for ClassPilot AI.

Call configure_logging() once at process startup (see main.py). Every other
module just does `logger = logging.getLogger(__name__)` and logs normally -
no print() statements anywhere in the codebase.
"""

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    if root.handlers:
        # Already configured (e.g. re-entrant call) - don't add duplicate handlers.
        return

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Quiet down noisy third-party libraries unless we're at DEBUG.
    if level.upper() != "DEBUG":
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)
        logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("apscheduler").setLevel(logging.WARNING)

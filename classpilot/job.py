"""
ClassPilot AI — Background Job Entrypoint

Standalone background script intended to be invoked periodically (e.g. via Cloud Scheduler
triggering a Cloud Run Job execution every 15 minutes).

1. Reads enabled watcher configurations from PostgreSQL.
2. For each enabled user, fetches their Google credentials.
3. Polls Google Classroom and checks deadline schedules.
4. Uses atomic StateStore claims to send notifications exactly once.
5. Exits when complete.
"""

import logging

from classpilot.config import get_config
from classpilot.logging_setup import configure_logging
from classpilot.user_store import UserStore
from classpilot.google_oauth import get_authorized_credentials
from classpilot.services import build_user_services
from classpilot.state_store import StateStore

logger = logging.getLogger(__name__)


def main() -> None:
    config = get_config()
    configure_logging(config.log_level)
    
    logger.info("Starting ClassPilot background job execution")
    state_store = StateStore()
    enabled_users = state_store.get_enabled_watchers()
    
    if not enabled_users:
        logger.info("No users have the watcher enabled. Exiting.")
        return
        
    user_store = UserStore()
    
    for user_id in enabled_users:
        user = user_store.get_user_by_id(user_id)
        if not user:
            logger.warning("User %s is enabled but does not exist in UserStore", user_id)
            continue
            
        try:
            credentials = get_authorized_credentials(user_id, user_store)
        except Exception as exc:
            logger.warning("Could not fetch credentials for user %s: %s", user_id, exc)
            continue
            
        logger.info("Running poll cycle for user %s", user_id)
        
        try:
            services = build_user_services(user_id, credentials, to_email=user.email)
            services.run_poll_cycle()
        except Exception as exc:
            logger.error("Poll cycle failed for user %s: %s", user_id, exc, exc_info=True)
            
    logger.info("Background job execution complete")


if __name__ == "__main__":
    main()

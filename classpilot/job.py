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


import sys

def run_diagnostics(user_id: str, credentials, config) -> None:
    """Run read-only verifications for all critical integrations."""
    logger.info("========================================")
    logger.info("DRY RUN DIAGNOSTICS FOR USER: %s", user_id)
    logger.info("========================================")
    
    # 1. Verify Classroom assignment fetching
    logger.info("1. Verifying Google Classroom connectivity...")
    try:
        from googleapiclient.discovery import build
        client = build("classroom", "v1", credentials=credentials)
        courses = client.courses().list(pageSize=1).execute()
        logger.info("   [PASS] Successfully fetched %d course(s)", len(courses.get('courses', [])))
    except Exception as exc:
        logger.error("   [FAIL] Classroom fetch failed: %s", exc)

    # 2. Verify Groq (LLM) configuration
    try:
        logger.info("2. Verifying LLM (%s) configuration...", config.llm_provider)
        from classpilot.llm import create_llm_provider
        from classpilot.events import EventType
        llm = create_llm_provider(config)
        response = llm.generate_message(EventType.NEW_ASSIGNMENT, "Diagnostic Test Assignment")
        if not response:
            raise ValueError("LLM returned empty response")
        logger.info("   [PASS] Successfully generated text using %s", config.llm_provider)
    except Exception as exc:
        logger.error("   [FAIL] LLM verification failed: %s", exc)

    # 3. Verify SMTP configuration
    try:
        logger.info("3. Verifying SMTP configuration...")
        if config.notifier_backend.lower() == "email":
            import smtplib
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15) as server:
                server.starttls()
                server.login(config.smtp_user, config.smtp_password)
            logger.info("   [PASS] Successfully connected and authenticated to SMTP server")
        else:
            logger.info("   [SKIP] Notifier backend is '%s', skipping SMTP verification", config.notifier_backend)
    except Exception as exc:
        logger.error("   [FAIL] SMTP verification failed: %s", exc)

def main() -> None:
    is_dry_run = "--dry-run" in sys.argv
    config = get_config()
    configure_logging(config.log_level)
    
    if is_dry_run:
        logger.info("Starting ClassPilot background job in DRY RUN mode")
    else:
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
            
        if is_dry_run:
            run_diagnostics(user_id, credentials, config)
        else:
            logger.info("Running poll cycle for user %s", user_id)
            try:
                services = build_user_services(user_id, credentials, to_email=user.email)
                services.run_poll_cycle()
            except Exception as exc:
                logger.error("Poll cycle failed for user %s: %s", user_id, exc, exc_info=True)
            
    if is_dry_run:
        logger.info("Dry run diagnostics complete")
    else:
        logger.info("Background job execution complete")


if __name__ == "__main__":
    main()

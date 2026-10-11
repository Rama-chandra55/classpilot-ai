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


import re
import sys


def _sanitize_diagnostic_message(msg: str) -> str:
    """Redact sensitive API keys or credentials from error messages."""
    msg = re.sub(r'([?&]key=)[^&\s]+', r'\1[REDACTED]', msg)
    msg = re.sub(r'AIza[0-9A-Za-z-_]{35}', '[REDACTED_API_KEY]', msg)
    msg = re.sub(r'Bearer\s+[A-Za-z0-9._~+/-]+', 'Bearer [REDACTED]', msg)
    return msg


def run_diagnostics(user_id: str, credentials, config) -> None:
    """Run read-only verifications for all critical integrations."""
    logger.info("========================================")
    logger.info("DRY RUN DIAGNOSTICS FOR USER: %s", user_id)
    logger.info("========================================")

    # 1. Verify Classroom assignment fetching
    logger.info("1. Verifying Google Classroom connectivity and assignment retrieval...")
    try:
        from googleapiclient.discovery import build
        client = build("classroom", "v1", credentials=credentials)

        raw_courses = []
        page_token = None
        max_course_pages = 3
        course_pages_fetched = 0

        while course_pages_fetched < max_course_pages:
            params = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            courses_result = client.courses().list(**params).execute()
            if isinstance(courses_result, dict):
                batch = courses_result.get("courses", [])
                if isinstance(batch, list):
                    raw_courses.extend(batch)
                page_token = courses_result.get("nextPageToken")
            else:
                page_token = None
            course_pages_fetched += 1
            if not page_token:
                break

        logger.info("   [PASS] Successfully fetched %d course(s)", len(raw_courses))

        passed_count = 0
        failed_count = 0
        skipped_count = 0

        for course in raw_courses:
            course_id = course.get("id") if isinstance(course, dict) else None
            course_name = (course.get("name") if isinstance(course, dict) else None) or "Unnamed Course"
            course_state = (course.get("courseState") if isinstance(course, dict) else None) or "ACTIVE"

            if not course_id:
                logger.info("   [SKIP] Course '%s': Missing course ID", course_name)
                skipped_count += 1
                continue

            # Skip non-active courses (e.g. ARCHIVED, DECLINED, PROVISIONED)
            if course_state != "ACTIVE":
                logger.info(
                    "   [SKIP] Course '%s' (id: %s): Course is not active (state: %s)",
                    course_name, course_id, course_state
                )
                skipped_count += 1
                continue

            # Attempt read-only coursework retrieval with pagination capped
            try:
                assignments_count = 0
                cw_page_token = None
                cw_pages_fetched = 0
                max_coursework_pages = 3

                while cw_pages_fetched < max_coursework_pages:
                    cw_params = {"courseId": course_id, "pageSize": 50}
                    if cw_page_token:
                        cw_params["pageToken"] = cw_page_token
                    cw_res = client.courses().courseWork().list(**cw_params).execute()
                    if isinstance(cw_res, dict):
                        cw_items = cw_res.get("courseWork", [])
                        if isinstance(cw_items, list):
                            assignments_count += len(cw_items)
                        cw_page_token = cw_res.get("nextPageToken")
                    else:
                        cw_page_token = None
                    cw_pages_fetched += 1
                    if not cw_page_token:
                        break

                if assignments_count > 0:
                    logger.info(
                        "   [PASS] Course '%s' (id: %s): Successfully retrieved %d assignment(s)",
                        course_name, course_id, assignments_count
                    )
                else:
                    logger.info(
                        "   [PASS] Course '%s' (id: %s): 0 assignments found (empty coursework list)",
                        course_name, course_id
                    )
                passed_count += 1

            except Exception as course_err:
                resp = getattr(course_err, "resp", None)
                status = getattr(resp, "status", None)
                is_403 = (
                    status == 403
                    or getattr(course_err, "status_code", None) == 403
                    or "403" in str(course_err)
                )
                if is_403:
                    reason = getattr(course_err, "reason", None) or "Permission denied"
                    logger.error(
                        "   [FAIL] Course '%s' (id: %s): HTTP 403 Forbidden - %s",
                        course_name, course_id, reason
                    )
                else:
                    sanitized_err = _sanitize_diagnostic_message(str(course_err))
                    logger.error(
                        "   [FAIL] Course '%s' (id: %s): CourseWork retrieval failed: %s",
                        course_name, course_id, sanitized_err
                    )
                failed_count += 1

        logger.info(
            "   Classroom coursework summary for user %s: %d passed, %d failed, %d skipped",
            user_id, passed_count, failed_count, skipped_count
        )

    except Exception as exc:
        sanitized_exc = _sanitize_diagnostic_message(str(exc))
        logger.error("   [FAIL] Classroom fetch failed: %s", sanitized_exc)


    # 2. Verify LLM configuration
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

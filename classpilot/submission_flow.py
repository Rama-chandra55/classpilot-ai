"""
Features 4 + 5: Submission Flow

Implements the two-step, confirmation-gated submission process:

  Feature 4 — Upload Confirmation
    1. find_assignment(query)   → AssignmentMatch  (fuzzy match across courses)
    2. find_best_file(name)     → FileMatch        (fuzzy match in Drive)
    3. attach_file(...)         → bool             (attach only, no turn-in)

  Feature 5 — Turn In Confirmation
    4. turn_in(...)             → bool             (turn in only, after user confirms)

The calling layer (CLI, chat, MCP tool) owns the confirmation prompts and
YES/NO gates — SubmissionFlow only executes when told to. It never turns in
without an explicit call to turn_in().

Fuzzy matching uses difflib.SequenceMatcher so "DBMS assignment" correctly
resolves to "DBMS Final Project" even with slight wording differences.
"""

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional

from .classroom_client import classroom, drive
from .events import EventType
from .notification_service import NotificationService

logger = logging.getLogger(__name__)

# Minimum similarity score (0–1) for a match to be considered valid.
_ASSIGNMENT_MATCH_THRESHOLD = 0.35
_FILE_MATCH_THRESHOLD = 0.30


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class AssignmentMatch:
    """A resolved assignment found by fuzzy search."""
    course_id: str
    course_name: str
    assignment_id: str
    title: str
    score: float          # 0.0–1.0 similarity score


@dataclass
class FileMatch:
    """A Drive file found by fuzzy search."""
    file_id: str
    file_name: str
    mime_type: str
    score: float          # 0.0–1.0 similarity score


@dataclass
class SubmissionResult:
    """Returned by attach_file() and turn_in() to describe the outcome."""
    success: bool
    state: Optional[str] = None       # e.g. "TURNED_IN", "CREATED"
    alternate_link: Optional[str] = None
    error: Optional[str] = None


# ── Core service ──────────────────────────────────────────────────────────────

class SubmissionFlow:
    """
    Orchestrates the find → confirm → attach → confirm → turn-in flow.

    This class contains no confirmation logic itself — it only executes
    API calls. The caller is responsible for showing prompts and gating
    on YES/NO.

    Feature 6: if notification_service is provided, a SUBMISSION_SUCCESS
    message is generated and sent automatically after a successful turn-in,
    so the caller never has to remember to do it.
    """

    def __init__(self, notification_service: NotificationService = None):
        self._notification_service = notification_service

    # ── Feature 4: find assignment ────────────────────────────────────────────

    def find_assignment(self, query: str) -> Optional[AssignmentMatch]:
        """
        Fuzzy-search all active courses for an assignment matching `query`.

        Returns the single best match above the similarity threshold,
        or None if nothing is close enough.

        Args:
            query: Free-text from the user, e.g. "DBMS assignment".
        """
        try:
            courses = classroom.list_courses(page_size=100, course_states=["ACTIVE"])
        except Exception:
            logger.error("Failed to list courses while searching for assignment", exc_info=True)
            return None

        best: Optional[AssignmentMatch] = None

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", "Unknown")
            if not course_id:
                continue

            try:
                assignments = classroom.list_assignments(course_id, page_size=100)
            except Exception:
                logger.error(
                    "Failed to list assignments for course %s", course_name, exc_info=True
                )
                continue

            for assignment in assignments:
                title = assignment.get("title", "")
                score = _similarity(query, title)
                if score >= _ASSIGNMENT_MATCH_THRESHOLD:
                    if best is None or score > best.score:
                        best = AssignmentMatch(
                            course_id=course_id,
                            course_name=course_name,
                            assignment_id=assignment["id"],
                            title=title,
                            score=score,
                        )

        if best:
            logger.info(
                "Assignment match: '%s' in %s (score=%.2f)",
                best.title, best.course_name, best.score,
            )
        else:
            logger.warning("No assignment found matching query: '%s'", query)

        return best

    # ── Feature 4: find best file ─────────────────────────────────────────────

    def find_best_file(self, assignment_name: str) -> Optional[FileMatch]:
        """
        Fuzzy-search Google Drive for a file whose name best matches the
        assignment name.

        Strategy:
          1. Search Drive using the first keyword from the assignment name.
          2. Score every result with SequenceMatcher.
          3. Return the highest-scoring file above the threshold.

        Args:
            assignment_name: Title of the assignment, e.g. "DBMS Final Project".
        """
        # Use the first meaningful word as the Drive search keyword so we
        # cast a wide net, then filter by score.
        keyword = _first_keyword(assignment_name)

        try:
            results = drive.search_files(query=keyword, page_size=50)
        except Exception:
            logger.error("Drive file search failed for keyword '%s'", keyword, exc_info=True)
            return None

        if not results:
            # Broaden: try listing recent files without a keyword filter
            try:
                results = drive.list_files(page_size=50)
            except Exception:
                logger.error("Drive list_files fallback also failed", exc_info=True)
                return None

        best: Optional[FileMatch] = None

        for f in results:
            name = f.get("name", "")
            score = _similarity(assignment_name, name)
            if score >= _FILE_MATCH_THRESHOLD:
                if best is None or score > best.score:
                    best = FileMatch(
                        file_id=f["id"],
                        file_name=name,
                        mime_type=f.get("mime_type", ""),
                        score=score,
                    )

        if best:
            logger.info(
                "File match: '%s' (score=%.2f) for assignment '%s'",
                best.file_name, best.score, assignment_name,
            )
        else:
            logger.warning("No file found matching assignment name: '%s'", assignment_name)

        return best

    # ── Feature 4: attach file (upload step) ─────────────────────────────────

    def attach_file(
        self,
        assignment: AssignmentMatch,
        file: FileMatch,
    ) -> SubmissionResult:
        """
        Attach `file` to the student's submission for `assignment`.
        Does NOT turn in — that requires a separate call to turn_in().

        Args:
            assignment: Resolved AssignmentMatch from find_assignment().
            file:       Resolved FileMatch from find_best_file().

        Returns:
            SubmissionResult with success=True and current submission state,
            or success=False with an error message.
        """
        submission_id = self._get_submission_id(
            assignment.course_id, assignment.assignment_id
        )
        if not submission_id:
            return SubmissionResult(
                success=False,
                error="Could not find your submission record for this assignment.",
            )

        try:
            result = classroom.attach_submission_files(
                course_id=assignment.course_id,
                assignment_id=assignment.assignment_id,
                submission_id=submission_id,
                attachments=[{"drive_file_id": file.file_id}],
            )
            logger.info(
                "File '%s' attached to '%s' (state=%s)",
                file.file_name, assignment.title, result.get("state"),
            )
            return SubmissionResult(
                success=True,
                state=result.get("state"),
                alternate_link=result.get("alternate_link") or result.get("alternateLink"),
            )
        except Exception as exc:
            logger.error(
                "Failed to attach file '%s' to '%s': %s",
                file.file_name, assignment.title, exc, exc_info=True,
            )
            return SubmissionResult(success=False, error=str(exc))

    # ── Feature 5: turn in ────────────────────────────────────────────────────

    def turn_in(self, assignment: AssignmentMatch) -> SubmissionResult:
        """
        Turn in the submission for `assignment`.
        Call this only after the user has confirmed.

        Args:
            assignment: Resolved AssignmentMatch from find_assignment().

        Returns:
            SubmissionResult with success=True and state="TURNED_IN",
            or success=False with an error message.
        """
        submission_id = self._get_submission_id(
            assignment.course_id, assignment.assignment_id
        )
        if not submission_id:
            return SubmissionResult(
                success=False,
                error="Could not find your submission record for this assignment.",
            )

        try:
            result = classroom.turn_in_submission(
                course_id=assignment.course_id,
                assignment_id=assignment.assignment_id,
                submission_id=submission_id,
            )
            logger.info(
                "Turned in '%s' (state=%s)", assignment.title, result.get("state")
            )
            # Feature 6: fire success notification automatically after confirmed turn-in
            self._notify_success(assignment.title)
            return SubmissionResult(
                success=True,
                state=result.get("state"),
                alternate_link=result.get("alternate_link") or result.get("alternateLink"),
            )
        except Exception as exc:
            logger.error(
                "Turn-in failed for '%s': %s", assignment.title, exc, exc_info=True
            )
            return SubmissionResult(success=False, error=str(exc))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _notify_success(self, assignment_name: str) -> None:
        """
        Feature 6: send a SUBMISSION_SUCCESS notification if a
        NotificationService was provided at construction time.
        Failures are logged but never propagate — the turn-in already
        succeeded and we must not mislead the caller about that.
        """
        if self._notification_service is None:
            return
        try:
            delivered = self._notification_service.notify(
                EventType.SUBMISSION_SUCCESS, assignment_name
            )
            if not delivered:
                logger.warning(
                    "Success notification could not be delivered for '%s'",
                    assignment_name,
                )
        except Exception:
            logger.error(
                "Unexpected error sending success notification for '%s'",
                assignment_name, exc_info=True,
            )

    def _get_submission_id(
        self, course_id: str, assignment_id: str
    ) -> Optional[str]:
        """
        Fetch the current user's submission ID for a given assignment.
        The Classroom API always creates exactly one submission per student.
        """
        try:
            submissions = classroom.list_submissions(
                course_id=course_id,
                assignment_id=assignment_id,
                page_size=1,
            )
            if submissions:
                return submissions[0]["id"]
            logger.warning(
                "No submission record found for assignment %s in course %s",
                assignment_id, course_id,
            )
            return None
        except Exception:
            logger.error(
                "Failed to fetch submission ID for assignment %s", assignment_id,
                exc_info=True,
            )
            return None


# ── Fuzzy matching helpers ────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    """
    Case-insensitive SequenceMatcher similarity score between two strings.
    Returns a float in [0.0, 1.0].
    """
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _first_keyword(text: str) -> str:
    """
    Extract the first meaningful word from a string to use as a Drive
    search keyword. Skips short stop-words like 'the', 'my', 'a'.
    """
    stop = {"the", "a", "an", "my", "our", "for", "of", "and", "or", "in"}
    words = text.split()
    for word in words:
        clean = word.strip(".,!?").lower()
        if clean and clean not in stop and len(clean) > 1:
            return clean
    return text.split()[0] if text.split() else text

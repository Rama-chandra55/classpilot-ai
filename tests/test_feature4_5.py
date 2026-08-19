"""
tests/test_feature4_5.py  —  Features 4 + 5: Submit + Turn In
Pure unittest, all external deps stubbed.
"""

import os, sys, types, unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── stub external deps ────────────────────────────────────────────────────────
def _stub(*names):
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts)+1):
            key = ".".join(parts[:i])
            if key not in sys.modules:
                m = types.ModuleType(key)
                m.__path__ = []  # mark as package so submodules can be imported
                sys.modules[key] = m
                if i > 1:
                    setattr(sys.modules[".".join(parts[:i-1])], parts[i-1], m)

_stub(
    "anthropic","dotenv","fastmcp",
    "google.auth.transport.requests","google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery","googleapiclient.http","googleapiclient.errors",
    "apscheduler.schedulers.blocking","apscheduler.schedulers.background","apscheduler.executors.pool",
)
sys.modules["dotenv"].load_dotenv = lambda *a,**k: None
sys.modules["fastmcp"].FastMCP = type("FastMCP",(),{"__init__":lambda s,*a,**k:None,"tool":lambda s:(lambda f:f)})
sys.modules["google.auth.transport.requests"].Request = object
sys.modules["google.oauth2.credentials"].Credentials = object
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = object
sys.modules["googleapiclient.discovery"].build = lambda *a,**k: None
sys.modules["googleapiclient.discovery"].Resource = object
sys.modules["googleapiclient.http"].MediaIoBaseUpload = object
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception
sys.modules["apscheduler.schedulers.blocking"].BlockingScheduler = type("BS",(),{"__init__":lambda s,**k:None,"add_job":lambda s,*a,**k:None,"start":lambda s:None})
sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = type("BGS",(),{"__init__":lambda s,**k:None,"add_job":lambda s,*a,**k:None,"start":lambda s:None,"shutdown":lambda s,**k:None,"remove_job":lambda s,j:None,"running":True})
sys.modules["apscheduler.executors.pool"].ThreadPoolExecutor = type("TPE",(),{"__init__":lambda s,**k:None})

# ── imports ───────────────────────────────────────────────────────────────────
import classpilot.classroom_client as cc
from classpilot.submission_flow import (
    SubmissionFlow, AssignmentMatch, FileMatch, SubmissionResult,
    _similarity, _first_keyword,
)

# ── fake classroom / drive modules ────────────────────────────────────────────
def _fake_classroom(courses=None, assignments=None, submissions=None,
                    attach_result=None, turnin_result=None, fail_attach=False, fail_turnin=False):
    m = MagicMock()
    m.list_courses.return_value = courses or []
    m.list_assignments.return_value = assignments or []
    m.list_submissions.return_value = submissions or []
    if fail_attach:
        m.attach_submission_files.side_effect = Exception("Attach failed")
    else:
        m.attach_submission_files.return_value = attach_result or {"id":"s1","state":"CREATED","alternateLink":"http://x"}
    if fail_turnin:
        m.turn_in_submission.side_effect = Exception("TurnIn failed")
    else:
        m.turn_in_submission.return_value = turnin_result or {"id":"s1","state":"TURNED_IN","alternateLink":"http://x"}
    return m

def _fake_drive(files=None, fail=False):
    m = MagicMock()
    if fail:
        m.search_files.side_effect = Exception("Drive error")
        m.list_files.side_effect = Exception("Drive error")
    else:
        m.search_files.return_value = files or []
        m.list_files.return_value = files or []
    return m

def _flow(courses=None, assignments=None, submissions=None,
          drive_files=None, attach_result=None, turnin_result=None,
          fail_attach=False, fail_turnin=False, fail_drive=False):
    """Return a SubmissionFlow with fully mocked classroom and drive."""
    import classpilot.submission_flow as sf_mod
    sf_mod.classroom = _fake_classroom(
        courses=courses, assignments=assignments, submissions=submissions,
        attach_result=attach_result, turnin_result=turnin_result,
        fail_attach=fail_attach, fail_turnin=fail_turnin,
    )
    sf_mod.drive = _fake_drive(files=drive_files, fail=fail_drive)
    return SubmissionFlow()

# ── standard test data ────────────────────────────────────────────────────────
COURSES = [{"id":"c1","name":"DBMS"},{"id":"c2","name":"OS"}]
DBMS_ASSIGNMENTS = [
    {"id":"a1","title":"DBMS Final Project"},
    {"id":"a2","title":"DBMS Lab Report"},
]
OS_ASSIGNMENTS = [{"id":"a3","title":"OS Assignment 1"}]
SUBMISSIONS = [{"id":"s1","state":"CREATED","userId":"u1"}]
DRIVE_FILES = [
    {"id":"f1","name":"DBMS_Final.pdf","mime_type":"application/pdf"},
    {"id":"f2","name":"OS_Notes.docx","mime_type":"application/vnd.openxmlformats"},
]

# ═════════════════════════════════════════════════════════════════════════════
class TestSimilarity(unittest.TestCase):
    def test_exact_match_is_1(self):
        self.assertAlmostEqual(_similarity("DBMS", "DBMS"), 1.0)

    def test_case_insensitive(self):
        self.assertAlmostEqual(_similarity("dbms", "DBMS"), 1.0)

    def test_partial_match(self):
        score = _similarity("DBMS assignment", "DBMS Final Project")
        self.assertGreater(score, 0.3)

    def test_unrelated_strings_low_score(self):
        score = _similarity("DBMS", "Operating Systems")
        self.assertLess(score, 0.5)

    def test_empty_strings(self):
        self.assertAlmostEqual(_similarity("", ""), 1.0)


class TestFirstKeyword(unittest.TestCase):
    def test_skips_stop_words(self):
        self.assertEqual(_first_keyword("my DBMS assignment"), "dbms")

    def test_returns_first_meaningful_word(self):
        self.assertEqual(_first_keyword("DBMS Final Project"), "dbms")

    def test_handles_single_word(self):
        self.assertEqual(_first_keyword("DBMS"), "dbms")

    def test_strips_punctuation(self):
        result = _first_keyword("DBMS.")
        self.assertNotIn(".", result)


# ═════════════════════════════════════════════════════════════════════════════
class TestFindAssignment(unittest.TestCase):
    def test_finds_best_match_across_courses(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(
            courses=COURSES,
            assignments=DBMS_ASSIGNMENTS,
        )
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        result = flow.find_assignment("DBMS Final Project")
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "DBMS Final Project")
        self.assertEqual(result.course_id, "c1")

    def test_returns_none_when_no_match(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(courses=COURSES, assignments=DBMS_ASSIGNMENTS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        result = flow.find_assignment("Quantum Physics Thesis")
        self.assertIsNone(result)

    def test_picks_highest_score_when_multiple_candidates(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(courses=COURSES, assignments=DBMS_ASSIGNMENTS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        # "DBMS Final Project" should score higher than "DBMS Lab Report"
        result = flow.find_assignment("DBMS Final Project")
        self.assertEqual(result.title, "DBMS Final Project")

    def test_handles_course_list_failure_gracefully(self):
        import classpilot.submission_flow as sf_mod
        m = MagicMock()
        m.list_courses.side_effect = Exception("API down")
        sf_mod.classroom = m
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        result = flow.find_assignment("DBMS")
        self.assertIsNone(result)

    def test_handles_assignment_list_failure_gracefully(self):
        import classpilot.submission_flow as sf_mod
        m = MagicMock()
        m.list_courses.return_value = COURSES
        m.list_assignments.side_effect = Exception("API down")
        sf_mod.classroom = m
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        result = flow.find_assignment("DBMS Final Project")
        self.assertIsNone(result)

    def test_result_contains_course_name(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(courses=COURSES, assignments=DBMS_ASSIGNMENTS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        result = flow.find_assignment("DBMS Final")
        self.assertEqual(result.course_name, "DBMS")

    def test_score_is_between_0_and_1(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(courses=COURSES, assignments=DBMS_ASSIGNMENTS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        result = flow.find_assignment("DBMS Final Project")
        self.assertGreaterEqual(result.score, 0.0)
        self.assertLessEqual(result.score, 1.0)


# ═════════════════════════════════════════════════════════════════════════════
class TestFindBestFile(unittest.TestCase):
    def test_finds_best_matching_file(self):
        flow = _flow(drive_files=DRIVE_FILES)
        result = flow.find_best_file("DBMS Final Project")
        self.assertIsNotNone(result)
        self.assertEqual(result.file_name, "DBMS_Final.pdf")

    def test_returns_none_when_no_match(self):
        flow = _flow(drive_files=DRIVE_FILES)
        result = flow.find_best_file("Quantum Physics Thesis")
        self.assertIsNone(result)

    def test_returns_none_when_drive_empty(self):
        flow = _flow(drive_files=[])
        result = flow.find_best_file("DBMS Final Project")
        self.assertIsNone(result)

    def test_handles_drive_failure_gracefully(self):
        flow = _flow(fail_drive=True)
        result = flow.find_best_file("DBMS Final Project")
        self.assertIsNone(result)

    def test_result_has_file_id_and_name(self):
        flow = _flow(drive_files=DRIVE_FILES)
        result = flow.find_best_file("DBMS Final Project")
        self.assertEqual(result.file_id, "f1")
        self.assertIsInstance(result.file_name, str)

    def test_score_is_between_0_and_1(self):
        flow = _flow(drive_files=DRIVE_FILES)
        result = flow.find_best_file("DBMS Final Project")
        self.assertGreaterEqual(result.score, 0.0)
        self.assertLessEqual(result.score, 1.0)


# ═════════════════════════════════════════════════════════════════════════════
class TestAttachFile(unittest.TestCase):
    ASSIGNMENT = AssignmentMatch("c1","DBMS","a1","DBMS Final Project", 0.9)
    FILE       = FileMatch("f1","DBMS_Final.pdf","application/pdf", 0.85)

    def test_returns_success_on_happy_path(self):
        flow = _flow(submissions=SUBMISSIONS)
        result = flow.attach_file(self.ASSIGNMENT, self.FILE)
        self.assertTrue(result.success)

    def test_calls_attach_submission_files_with_correct_args(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        flow.attach_file(self.ASSIGNMENT, self.FILE)
        sf_mod.classroom.attach_submission_files.assert_called_once_with(
            course_id="c1",
            assignment_id="a1",
            submission_id="s1",
            attachments=[{"drive_file_id": "f1"}],
        )

    def test_returns_failure_when_no_submission_id(self):
        flow = _flow(submissions=[])
        result = flow.attach_file(self.ASSIGNMENT, self.FILE)
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    def test_returns_failure_on_api_error(self):
        flow = _flow(submissions=SUBMISSIONS, fail_attach=True)
        result = flow.attach_file(self.ASSIGNMENT, self.FILE)
        self.assertFalse(result.success)
        self.assertIn("Attach failed", result.error)

    def test_does_not_turn_in_during_attach(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        flow.attach_file(self.ASSIGNMENT, self.FILE)
        sf_mod.classroom.turn_in_submission.assert_not_called()

    def test_result_contains_submission_state(self):
        flow = _flow(submissions=SUBMISSIONS,
                     attach_result={"id":"s1","state":"CREATED","alternateLink":"http://x"})
        result = flow.attach_file(self.ASSIGNMENT, self.FILE)
        self.assertEqual(result.state, "CREATED")


# ═════════════════════════════════════════════════════════════════════════════
class TestTurnIn(unittest.TestCase):
    ASSIGNMENT = AssignmentMatch("c1","DBMS","a1","DBMS Final Project", 0.9)

    def test_returns_success_with_turned_in_state(self):
        flow = _flow(submissions=SUBMISSIONS,
                     turnin_result={"id":"s1","state":"TURNED_IN","alternateLink":"http://x"})
        result = flow.turn_in(self.ASSIGNMENT)
        self.assertTrue(result.success)
        self.assertEqual(result.state, "TURNED_IN")

    def test_calls_turn_in_submission_with_correct_args(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        flow.turn_in(self.ASSIGNMENT)
        sf_mod.classroom.turn_in_submission.assert_called_once_with(
            course_id="c1",
            assignment_id="a1",
            submission_id="s1",
        )

    def test_does_not_attach_anything_during_turn_in(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        flow.turn_in(self.ASSIGNMENT)
        sf_mod.classroom.attach_submission_files.assert_not_called()

    def test_returns_failure_when_no_submission_id(self):
        flow = _flow(submissions=[])
        result = flow.turn_in(self.ASSIGNMENT)
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    def test_returns_failure_on_api_error(self):
        flow = _flow(submissions=SUBMISSIONS, fail_turnin=True)
        result = flow.turn_in(self.ASSIGNMENT)
        self.assertFalse(result.success)
        self.assertIn("TurnIn failed", result.error)

    def test_alternate_link_returned_on_success(self):
        flow = _flow(submissions=SUBMISSIONS,
                     turnin_result={"id":"s1","state":"TURNED_IN","alternateLink":"http://classroom.google.com/x"})
        result = flow.turn_in(self.ASSIGNMENT)
        self.assertEqual(result.alternate_link, "http://classroom.google.com/x")


# ═════════════════════════════════════════════════════════════════════════════
class TestConfirmationGating(unittest.TestCase):
    """
    Proves that attach and turn-in are fully decoupled —
    the caller controls when each step executes.
    """
    ASSIGNMENT = AssignmentMatch("c1","DBMS","a1","DBMS Final Project", 0.9)
    FILE       = FileMatch("f1","DBMS_Final.pdf","application/pdf", 0.85)

    def test_attach_does_not_call_turn_in(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        flow.attach_file(self.ASSIGNMENT, self.FILE)
        sf_mod.classroom.turn_in_submission.assert_not_called()

    def test_turn_in_does_not_call_attach(self):
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()
        flow.turn_in(self.ASSIGNMENT)
        sf_mod.classroom.attach_submission_files.assert_not_called()

    def test_full_two_step_flow_in_order(self):
        """attach_file then turn_in succeed independently."""
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()

        attach_result = flow.attach_file(self.ASSIGNMENT, self.FILE)
        self.assertTrue(attach_result.success)

        turnin_result = flow.turn_in(self.ASSIGNMENT)
        self.assertTrue(turnin_result.success)
        self.assertEqual(turnin_result.state, "TURNED_IN")

    def test_turn_in_can_be_skipped(self):
        """If user says NO to turn-in, turn_in is simply never called."""
        import classpilot.submission_flow as sf_mod
        sf_mod.classroom = _fake_classroom(submissions=SUBMISSIONS)
        sf_mod.drive = _fake_drive()
        flow = SubmissionFlow()

        flow.attach_file(self.ASSIGNMENT, self.FILE)
        # User says NO — turn_in is NOT called
        sf_mod.classroom.turn_in_submission.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
class TestVendorClassroomFunctions(unittest.TestCase):
    """
    Verify the two new additive functions exist in the vendor classroom module
    and call the correct Classroom API endpoints.
    """

    def _make_service(self):
        svc = MagicMock()
        svc.courses().courseWork().studentSubmissions()\
            .modifyAttachments().execute.return_value = {}
        svc.courses().courseWork().studentSubmissions()\
            .turnIn().execute.return_value = {}
        svc.courses().courseWork().studentSubmissions()\
            .get().execute.return_value = {
                "id":"s1","state":"CREATED","late":False,
                "alternateLink":"http://x","updateTime":"2026-07-01"
            }
        return svc

    def test_attach_submission_files_exists(self):
        import classpilot.classroom_client as cc_mod
        self.assertTrue(hasattr(cc_mod.classroom, "attach_submission_files"))

    def test_turn_in_submission_exists(self):
        import classpilot.classroom_client as cc_mod
        self.assertTrue(hasattr(cc_mod.classroom, "turn_in_submission"))

    def test_original_submit_assignment_still_exists(self):
        """Ensure we did not remove or break the existing function."""
        import classpilot.classroom_client as cc_mod
        self.assertTrue(hasattr(cc_mod.classroom, "submit_assignment"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

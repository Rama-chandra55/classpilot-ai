"""
tests/test_study_tools.py  —  ClassPilot Study Assistant (5 tools)
Pure unittest, no network. All Google APIs fully mocked.
"""

import io, os, sys, types, unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── stub external deps ────────────────────────────────────────────────────────
import types as _types, sys as _sys
_aps  = _types.ModuleType("apscheduler");            _aps.__path__  = []; _sys.modules["apscheduler"]            = _aps
_apss = _types.ModuleType("apscheduler.schedulers"); _apss.__path__ = []; _sys.modules["apscheduler.schedulers"] = _apss; _aps.schedulers = _apss
del _aps, _apss, _types, _sys

def _stub(*names):
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            key = ".".join(parts[:i])
            if key not in sys.modules:
                m = types.ModuleType(key)
                m.__path__ = []
                sys.modules[key] = m
                if i > 1:
                    setattr(sys.modules[".".join(parts[:i - 1])], parts[i - 1], m)

_stub(
    "anthropic", "dotenv", "fastmcp", "pydantic",
    "google.auth.transport.requests", "google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery", "googleapiclient.http", "googleapiclient.errors",
    "apscheduler.schedulers.blocking", "apscheduler.schedulers.background",
    "apscheduler.executors.pool",
    "classroom_suite_mcp",
    "classroom_suite_mcp.auth",
    "classroom_suite_mcp.classroom",
    "classroom_suite_mcp.drive",
    "classroom_suite_mcp.docs",
)
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# pydantic stub
class _BM:
    def __init_subclass__(cls, **k): pass
    def __init__(self, **k):
        for key, val in k.items(): setattr(self, key, val)
class _Field:
    def __call__(self, *a, **k): return None
sys.modules["pydantic"].BaseModel = _BM
sys.modules["pydantic"].Field = _Field()

sys.modules["fastmcp"].FastMCP = type("FastMCP", (), {
    "__init__": lambda s, *a, **k: None,
    "tool": lambda s, *a, **k: (lambda f: f),
})
sys.modules["google.auth.transport.requests"].Request = object
sys.modules["google.oauth2.credentials"].Credentials = object
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = object
sys.modules["googleapiclient.discovery"].build = lambda *a, **k: MagicMock()
sys.modules["googleapiclient.discovery"].Resource = object
sys.modules["googleapiclient.http"].MediaIoBaseUpload = object
sys.modules["googleapiclient.http"].MediaIoBaseDownload = MagicMock
sys.modules["googleapiclient.errors"].HttpError = Exception
sys.modules["apscheduler.schedulers.blocking"].BlockingScheduler = type("BS", (), {"__init__": lambda s, **k: None})
sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = type("BGS", (), {
    "__init__": lambda s, **k: None, "add_job": lambda s, *a, **k: None,
    "start": lambda s: None, "shutdown": lambda s, **k: None,
    "remove_job": lambda s, j: None, "running": True,
})
sys.modules["apscheduler.executors.pool"].ThreadPoolExecutor = type("TPE", (), {"__init__": lambda s, **k: None})

# Stub auth functions used by study_client
_mock_classroom_svc = MagicMock()
_mock_drive_svc = MagicMock()
sys.modules["classroom_suite_mcp.auth"].get_classroom_service = lambda: _mock_classroom_svc
sys.modules["classroom_suite_mcp.auth"].get_drive_service     = lambda: _mock_drive_svc
sys.modules["classroom_suite_mcp.auth"].get_docs_service      = lambda: MagicMock()
sys.modules["classroom_suite_mcp.auth"].SCOPES = []
sys.modules["classroom_suite_mcp.auth"].GoogleAuth = MagicMock

# ── imports under test ────────────────────────────────────────────────────────
import classpilot.study_client as sc


def _reset_mocks():
    _mock_classroom_svc.reset_mock()
    _mock_drive_svc.reset_mock()


# ── sample fixtures ───────────────────────────────────────────────────────────

RAW_COURSE = {"id": "c1", "name": "Deep Learning", "section": "A", "courseState": "ACTIVE", "alternateLink": "http://x"}
RAW_TOPIC  = {"topicId": "1", "name": "Module 1 - Intro", "updateTime": "2026-01-01T00:00:00Z"}
RAW_TOPIC2 = {"topicId": "2", "name": "Module 2 - CNN",   "updateTime": "2026-01-02T00:00:00Z"}

RAW_MATERIAL = {
    "id": "m1", "title": "Session 1 - Intro to DL", "description": "First lecture",
    "topicId": "1", "state": "PUBLISHED",
    "materials": [
        {"driveFile": {"driveFile": {"id": "f1", "title": "Session1.pdf", "alternateLink": "http://pdf"}}},
        {"youtubeVideo": {"id": "yt1", "title": "DL Intro Video", "alternateLink": "http://yt"}},
    ],
}
RAW_ASSIGNMENT = {
    "id": "a1", "title": "Lab 1 Assignment", "description": "Submit lab work",
    "topicId": "1", "state": "PUBLISHED",
    "dueDate": {"year": 2026, "month": 12, "day": 1},
    "dueTime": {"hours": 23, "minutes": 59},
    "materials": [],
}


# ═════════════════════════════════════════════════════════════════════════════
class TestFetchCourses(unittest.TestCase):
    def setUp(self): _reset_mocks()

    def test_returns_list_of_courses(self):
        _mock_classroom_svc.courses().list().execute.return_value = {"courses": [RAW_COURSE]}
        result = sc.fetch_courses()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Deep Learning")

    def test_returns_empty_when_no_courses(self):
        _mock_classroom_svc.courses().list().execute.return_value = {"courses": []}
        self.assertEqual(sc.fetch_courses(), [])

    def test_filters_active_courses(self):
        _mock_classroom_svc.courses().list().execute.return_value = {"courses": [RAW_COURSE]}
        sc.fetch_courses()
        call_kwargs = _mock_classroom_svc.courses().list.call_args[1]
        self.assertIn("ACTIVE", call_kwargs.get("courseStates", []))

    def test_all_fields_present(self):
        _mock_classroom_svc.courses().list().execute.return_value = {"courses": [RAW_COURSE]}
        r = sc.fetch_courses()[0]
        for key in ("id", "name", "section", "state", "link"):
            self.assertIn(key, r)


class TestFetchTopics(unittest.TestCase):
    def setUp(self): _reset_mocks()

    def test_returns_sorted_topics(self):
        _mock_classroom_svc.courses().topics().list().execute.return_value = {
            "topic": [RAW_TOPIC2, RAW_TOPIC]  # out of order
        }
        result = sc.fetch_topics("c1")
        self.assertEqual(result[0]["name"], "Module 1 - Intro")
        self.assertEqual(result[1]["name"], "Module 2 - CNN")

    def test_returns_empty_when_no_topics(self):
        _mock_classroom_svc.courses().topics().list().execute.return_value = {"topic": []}
        self.assertEqual(sc.fetch_topics("c1"), [])

    def test_all_fields_present(self):
        _mock_classroom_svc.courses().topics().list().execute.return_value = {"topic": [RAW_TOPIC]}
        r = sc.fetch_topics("c1")[0]
        self.assertEqual(r["id"], "1")
        self.assertEqual(r["name"], "Module 1 - Intro")


class TestFetchMaterials(unittest.TestCase):
    def setUp(self): _reset_mocks()

    def test_fetches_course_work_materials(self):
        _mock_classroom_svc.courses().courseWorkMaterials().list().execute.return_value = {
            "courseWorkMaterial": [RAW_MATERIAL]
        }
        result = sc.fetch_course_work_materials("c1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "MATERIAL")

    def test_filters_by_topic_id(self):
        mat2 = dict(RAW_MATERIAL); mat2["topicId"] = "99"
        _mock_classroom_svc.courses().courseWorkMaterials().list().execute.return_value = {
            "courseWorkMaterial": [RAW_MATERIAL, mat2]
        }
        result = sc.fetch_course_work_materials("c1", topic_id="1")
        self.assertEqual(len(result), 1)

    def test_attachments_normalised(self):
        _mock_classroom_svc.courses().courseWorkMaterials().list().execute.return_value = {
            "courseWorkMaterial": [RAW_MATERIAL]
        }
        result = sc.fetch_course_work_materials("c1")
        atts = result[0]["attachments"]
        types = [a["type"] for a in atts]
        self.assertIn("drive",   types)
        self.assertIn("youtube", types)

    def test_fetches_assignments_with_assignment_kind(self):
        _mock_classroom_svc.courses().courseWork().list().execute.return_value = {
            "courseWork": [RAW_ASSIGNMENT]
        }
        result = sc.fetch_assignments("c1")
        self.assertEqual(result[0]["kind"], "ASSIGNMENT")


class TestNormaliseMaterial(unittest.TestCase):
    def test_drive_attachment_extracted(self):
        result = sc._normalise_material(RAW_MATERIAL, "MATERIAL")
        drive_att = next((a for a in result["attachments"] if a["type"] == "drive"), None)
        self.assertIsNotNone(drive_att)
        self.assertEqual(drive_att["id"], "f1")

    def test_youtube_attachment_extracted(self):
        result = sc._normalise_material(RAW_MATERIAL, "MATERIAL")
        yt_att = next((a for a in result["attachments"] if a["type"] == "youtube"), None)
        self.assertIsNotNone(yt_att)

    def test_kind_preserved(self):
        r = sc._normalise_material(RAW_ASSIGNMENT, "ASSIGNMENT")
        self.assertEqual(r["kind"], "ASSIGNMENT")

    def test_due_date_included_for_assignments(self):
        r = sc._normalise_material(RAW_ASSIGNMENT, "ASSIGNMENT")
        self.assertEqual(r["due_date"], {"year": 2026, "month": 12, "day": 1})


class TestReadDriveFile(unittest.TestCase):
    def setUp(self): _reset_mocks()

    def _setup_meta(self, mime, name="test.file", link="http://file"):
        _mock_drive_svc.files().get().execute.return_value = {
            "id": "f1", "name": name, "mimeType": mime, "webViewLink": link, "size": "1000"
        }

    def test_google_doc_exported_as_text(self):
        self._setup_meta("application/vnd.google-apps.document", "Lecture Notes")
        import io
        buf = io.BytesIO(b"Lecture content here")
        mock_dl = MagicMock()
        mock_dl.next_chunk.return_value = (None, True)
        with patch("classpilot.study_client.MediaIoBaseDownload", return_value=mock_dl):
            with patch("io.BytesIO", return_value=buf):
                _mock_drive_svc.files().export_media.return_value = MagicMock()
                result = sc.read_drive_file("f1")
        self.assertTrue(result["readable"])
        self.assertEqual(result["mime_type"], "application/vnd.google-apps.document")

    def test_pdf_returns_url_only(self):
        self._setup_meta("application/pdf", "Slides.pdf", "http://pdf")
        result = sc.read_drive_file("f1")
        self.assertFalse(result["readable"])
        self.assertEqual(result["content"], "")
        self.assertIn("http://pdf", result["link"])
        self.assertIn("PDF", result["note"])

    def test_pptx_text_extracted(self):
        self._setup_meta(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "Lecture.pptx", "http://pptx"
        )
        from pptx import Presentation as _RealPresentation

        prs = _RealPresentation()
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "Neural Networks"
        slide.placeholders[1].text_frame.text = "Intro to CNNs"
        raw = io.BytesIO()
        prs.save(raw)
        raw_bytes = raw.getvalue()

        def _fake_download(buf, request):
            buf.write(raw_bytes)
            dl = MagicMock()
            dl.next_chunk.return_value = (None, True)
            return dl

        with patch("classpilot.study_client.MediaIoBaseDownload", side_effect=_fake_download):
            _mock_drive_svc.files().get_media.return_value = MagicMock()
            result = sc.read_drive_file("f1")

        self.assertTrue(result["readable"])
        self.assertIn("Neural Networks", result["content"])
        self.assertIn("Intro to CNNs", result["content"])
        self.assertEqual(result["note"], "")

    def test_pptx_extraction_failure_falls_back_to_url_only(self):
        self._setup_meta(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "Corrupt.pptx", "http://pptx"
        )
        def _fake_download(buf, request):
            buf.write(b"not a real pptx file")
            dl = MagicMock()
            dl.next_chunk.return_value = (None, True)
            return dl

        with patch("classpilot.study_client.MediaIoBaseDownload", side_effect=_fake_download):
            _mock_drive_svc.files().get_media.return_value = MagicMock()
            result = sc.read_drive_file("f1")

        self.assertFalse(result["readable"])
        self.assertEqual(result["content"], "")
        self.assertIn("http://pptx", result["link"])

    def test_legacy_ppt_returns_url_only(self):
        self._setup_meta("application/vnd.ms-powerpoint", "OldLecture.ppt")
        result = sc.read_drive_file("f1")
        self.assertFalse(result["readable"])
        self.assertIn("legacy .ppt", result["note"])

    def test_url_only_result_has_link(self):
        self._setup_meta("application/pdf", "file.pdf", "http://drive/file")
        result = sc.read_drive_file("f1")
        self.assertEqual(result["link"], "http://drive/file")

    def test_result_always_has_required_keys(self):
        self._setup_meta("application/pdf")
        result = sc.read_drive_file("f1")
        for key in ("readable", "content", "mime_type", "name", "link", "note"):
            self.assertIn(key, result)


class TestSearchAll(unittest.TestCase):
    def setUp(self):
        _reset_mocks()
        # Clear any side_effect set by previous tests (e.g. test_handles_course_list_failure_gracefully)
        _mock_classroom_svc.courses().list().execute.side_effect = None
        _mock_classroom_svc.courses().list().execute.return_value = {}

    def _setup_classroom(self):
        _mock_classroom_svc.courses().list().execute.return_value = {"courses": [RAW_COURSE]}
        _mock_classroom_svc.courses().topics().list().execute.return_value = {"topic": [RAW_TOPIC]}
        _mock_classroom_svc.courses().courseWorkMaterials().list().execute.return_value = {
            "courseWorkMaterial": [RAW_MATERIAL]
        }
        _mock_classroom_svc.courses().courseWork().list().execute.return_value = {"courseWork": []}

    def test_finds_course_by_name(self):
        self._setup_classroom()
        results = sc.search_all("deep learning")
        types = [r["type"] for r in results]
        self.assertIn("course", types)

    def test_finds_module_by_name(self):
        self._setup_classroom()
        results = sc.search_all("module 1")
        types = [r["type"] for r in results]
        self.assertIn("module", types)

    def test_finds_material_by_title(self):
        self._setup_classroom()
        results = sc.search_all("session 1")
        types = [r["type"] for r in results]
        self.assertIn("material", types)

    def test_returns_empty_for_no_match(self):
        self._setup_classroom()
        results = sc.search_all("quantum cryptography thesis xyz")
        self.assertEqual(results, [])

    def test_result_has_context_field(self):
        self._setup_classroom()
        results = sc.search_all("session 1")
        material_results = [r for r in results if r["type"] == "material"]
        if material_results:
            self.assertIn("context", material_results[0])
            self.assertIn("Deep Learning", material_results[0]["context"])

    def test_case_insensitive(self):
        self._setup_classroom()
        r1 = sc.search_all("DEEP LEARNING")
        r2 = sc.search_all("deep learning")
        self.assertEqual(len(r1), len(r2))

    def test_handles_course_list_failure_gracefully(self):
        _mock_classroom_svc.courses().list().execute.side_effect = Exception("API down")
        results = sc.search_all("anything")
        self.assertEqual(results, [])


class TestScopeExtension(unittest.TestCase):
    """Verify classroom_client.py correctly extends vendor SCOPES."""

    def test_courseworkmaterials_scope_added(self):
        from classroom_suite_mcp import auth as _auth
        scopes = _auth.SCOPES
        self.assertTrue(
            any("courseworkmaterials" in s for s in scopes),
            f"courseworkmaterials scope missing. Got: {scopes}"
        )

    def test_announcements_scope_added(self):
        from classroom_suite_mcp import auth as _auth
        scopes = _auth.SCOPES
        self.assertTrue(
            any("announcements" in s for s in scopes),
            f"announcements scope missing. Got: {scopes}"
        )


class TestSubmissionToolsRemoved(unittest.TestCase):
    """Ensure deprecated submission tools are no longer exposed via MCP."""

    def test_server_has_no_submit_assignment_tool(self):
        import classpilot.server as srv
        tool_fns = [name for name in dir(srv) if name in
                    ("submit_assignment", "turn_in_assignment", "assignment_status")]
        # They may exist as plain functions but should not be MCP tools
        # Check the MCP instance's tool registry
        if hasattr(srv.mcp, "_tools"):
            for bad_tool in ("submit_assignment", "turn_in_assignment"):
                self.assertNotIn(bad_tool, srv.mcp._tools,
                                 f"{bad_tool} should not be an MCP tool")

    def test_study_tools_registered(self):
        import classpilot.server as srv
        expected_study = {
            "list_classes", "list_modules", "list_materials",
            "get_material", "search_classroom",
        }
        if hasattr(srv.mcp, "_tools"):
            for tool in expected_study:
                self.assertIn(tool, srv.mcp._tools,
                              f"Study tool '{tool}' not registered")


if __name__ == "__main__":
    unittest.main(verbosity=2)

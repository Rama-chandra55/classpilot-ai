"""
tests/test_scope_audit.py

Validates classpilot/classroom_client.py's scope-curation logic against the
REAL vendored classroom_suite_mcp package (not a stub) — this is the only
way to genuinely prove the "remove unused scopes, narrow drive to
readonly, add study scopes" logic works starting from the vendor's actual
default SCOPES list, since tests/test_study_tools.py's stub environment
starts from an empty SCOPES list (nothing to remove/narrow FROM there).

Self-contained: deliberately does NOT stub classroom_suite_mcp/auth,
mirroring the same real-vendor-import approach tests/test_feature4_5.py
already uses elsewhere in this suite. Only the handful of *other*
dependencies classpilot.classroom_client's import chain needs (fastmcp,
pydantic, google-api-client, etc., pulled in transitively via
classpilot.study_client / classpilot.server) are stubbed, so this file can
run without real Google credentials or network access.
"""

import sys
import types
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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


# Force a clean re-import of classroom_client + vendor auth: other test
# files in this suite may have already imported (and scope-patched) these
# modules under a different stub state, and Python caches modules
# process-wide once imported — see the identical rationale documented at
# the top of tests/test_study_tools.py for why this is necessary.
for _mod in ("classpilot.classroom_client", "classroom_suite_mcp", "classroom_suite_mcp.auth"):
    sys.modules.pop(_mod, None)

_stub(
    "anthropic", "dotenv", "fastmcp", "pydantic",
    "google.auth.transport.requests", "google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery", "googleapiclient.http", "googleapiclient.errors",
)
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
sys.modules["google.auth.transport.requests"].Request = object
sys.modules["google.oauth2.credentials"].Credentials = object
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = object
sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
sys.modules["googleapiclient.discovery"].Resource = object
sys.modules["googleapiclient.http"].MediaIoBaseUpload = object
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception

import classpilot.classroom_client  # noqa: E402  (triggers the real, unmocked scope patch)
from classroom_suite_mcp import auth as real_auth  # noqa: E402


class TestScopeAuditAgainstRealVendorDefaults(unittest.TestCase):
    """
    The definitive test: starting from the REAL vendor package's default
    SCOPES list (not a test stub), after classpilot.classroom_client has
    patched it, the result must be exactly the curated 6-scope set —
    nothing more, nothing less.
    """

    EXPECTED_SCOPES = {
        "https://www.googleapis.com/auth/classroom.courses.readonly",
        "https://www.googleapis.com/auth/classroom.coursework.me",
        "https://www.googleapis.com/auth/classroom.courseworkmaterials",
        "https://www.googleapis.com/auth/classroom.announcements",
        "https://www.googleapis.com/auth/classroom.topics.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    }

    def test_final_scope_set_is_exactly_the_curated_list(self):
        self.assertEqual(set(real_auth.SCOPES), self.EXPECTED_SCOPES)

    def test_no_duplicate_scopes(self):
        self.assertEqual(len(real_auth.SCOPES), len(set(real_auth.SCOPES)))

    # ---------- removed scopes ----------

    def test_full_drive_scope_removed(self):
        self.assertNotIn("https://www.googleapis.com/auth/drive", real_auth.SCOPES)

    def test_documents_scope_removed(self):
        self.assertFalse(any("documents" in s for s in real_auth.SCOPES))

    def test_rosters_scope_removed(self):
        self.assertFalse(any("rosters" in s for s in real_auth.SCOPES))

    def test_coursework_students_scope_removed(self):
        self.assertFalse(any("coursework.students" in s for s in real_auth.SCOPES))

    # ---------- narrowed scope ----------

    def test_drive_readonly_present_instead_of_full_drive(self):
        self.assertIn("https://www.googleapis.com/auth/drive.readonly", real_auth.SCOPES)

    # ---------- kept / added scopes needed by the exposed Study Assistant tools ----------

    def test_courses_readonly_kept(self):
        self.assertIn("https://www.googleapis.com/auth/classroom.courses.readonly", real_auth.SCOPES)

    def test_coursework_me_kept(self):
        self.assertIn("https://www.googleapis.com/auth/classroom.coursework.me", real_auth.SCOPES)

    def test_courseworkmaterials_added(self):
        self.assertIn("https://www.googleapis.com/auth/classroom.courseworkmaterials", real_auth.SCOPES)

    def test_announcements_added(self):
        self.assertIn("https://www.googleapis.com/auth/classroom.announcements", real_auth.SCOPES)

    def test_topics_readonly_added(self):
        self.assertIn("https://www.googleapis.com/auth/classroom.topics.readonly", real_auth.SCOPES)


if __name__ == "__main__":
    unittest.main()

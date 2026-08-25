"""
Classroom Client

Thin wrapper around the untouched `classroom_suite_mcp` package.

Scope curation (vendor auth.py is not modified):
  The vendor SCOPES list is patched here, before the global auth singleton
  is first created, to exactly the set of scopes ClassPilot's exposed
  tools actually use — no more, no less. Google's OAuth verification
  review and the blast radius of a compromised token both scale with
  requested scope, so this list is deliberately minimal.

  Added (needed by the Study Assistant, not present in the vendor default):
    - classroom.courseworkmaterials  → teacher-posted study materials
    - classroom.announcements        → course announcements
    - classroom.topics.readonly      → modules/topics

  Removed from the vendor default (audited — grep the codebase to confirm
  before ever re-adding one of these):
    - classroom.coursework.students  → only referenced by the vendored,
      UNEXPOSED list_submissions() (no `userId="me"` filter — that's a
      teacher-level "see every student's submissions" call, not something
      a student-facing app reading its own coursework needs). If a future
      phase re-exposes per-student submission listing, this scope comes
      back with it — deliberately, not by default.
    - classroom.rosters.readonly     → zero API calls anywhere in this
      codebase touch a roster endpoint. Entirely unused.
    - documents (Google Docs API)    → study_client.py reads Google Docs
      content via Drive's export_media, never via the Docs API itself.
      get_docs_service() is imported but never called.

  Narrowed:
    - drive (full read/write) → drive.readonly. Every active Drive call in
      study_client.py is read-only (files().get, .export_media, .get_media).
      Full `drive` write access was only needed by the vendored, UNEXPOSED
      submission-upload flow (classroom_suite_mcp.drive.upload_file /
      create_folder / delete_file, via classpilot/submission_flow.py) —
      consistent with that flow staying unregistered as an MCP tool.

  The user must delete token.json and re-authenticate ONCE after this
  change so Google issues a token that matches the new scope list exactly
  (Google will otherwise keep serving a token authorized for the old,
  broader set).
"""

import sys
from pathlib import Path

_VENDOR_SRC = Path(__file__).resolve().parent.parent / "vendor_classroom_suite_mcp" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

# ── Curate scopes before any auth singleton is created ────────────────────────
# This is the only permitted way to change scopes without touching vendor code.
from classroom_suite_mcp import auth as _auth_module

_UNUSED_VENDOR_SCOPES = {
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/documents",
}
_FULL_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_STUDY_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courseworkmaterials",
    "https://www.googleapis.com/auth/classroom.announcements",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
]

_auth_module.SCOPES = [
    s for s in _auth_module.SCOPES
    if s not in _UNUSED_VENDOR_SCOPES and s != _FULL_DRIVE_SCOPE
]
if _DRIVE_READONLY_SCOPE not in _auth_module.SCOPES:
    _auth_module.SCOPES.append(_DRIVE_READONLY_SCOPE)
for _scope in _STUDY_SCOPES:
    if _scope not in _auth_module.SCOPES:
        _auth_module.SCOPES.append(_scope)

# ── Re-export the original, unmodified vendor modules ─────────────────────────
from classroom_suite_mcp import classroom  # noqa: E402
from classroom_suite_mcp import drive       # noqa: E402
from classroom_suite_mcp import docs        # noqa: E402

__all__ = ["classroom", "drive", "docs"]


"""
Classroom Client

Thin wrapper around the untouched `classroom_suite_mcp` package.

Scope extension (vendor auth.py is not modified):
  The vendor SCOPES list is patched here, before the global auth singleton
  is first created, to add two scopes required for the Study Assistant:
    - classroom.courseworkmaterials  → teacher-posted study materials
    - classroom.announcements        → course announcements

  The user must delete token.json and re-authenticate ONCE after this change
  so Google issues a token that includes the new scopes.
"""

import sys
from pathlib import Path

_VENDOR_SRC = Path(__file__).resolve().parent.parent / "vendor_classroom_suite_mcp" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

# ── Extend scopes before any auth singleton is created ────────────────────────
# This is the only permitted way to add scopes without touching vendor code.
from classroom_suite_mcp import auth as _auth_module

_STUDY_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courseworkmaterials",
    "https://www.googleapis.com/auth/classroom.announcements",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
]
for _scope in _STUDY_SCOPES:
    if _scope not in _auth_module.SCOPES:
        _auth_module.SCOPES.append(_scope)

# ── Re-export the original, unmodified vendor modules ─────────────────────────
from classroom_suite_mcp import classroom  # noqa: E402
from classroom_suite_mcp import drive       # noqa: E402
from classroom_suite_mcp import docs        # noqa: E402

__all__ = ["classroom", "drive", "docs"]

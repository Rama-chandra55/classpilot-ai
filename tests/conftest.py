"""
conftest.py

Stubs all external dependencies (anthropic SDK, Google APIs, APScheduler)
at the sys.modules level before any ClassPilot module is imported. This
lets the full test suite run with no credentials and no network access.
"""

import sys
import types
import pytest


def _stub_module(*names):
    """Create empty stub modules for each dotted path, wiring parent attrs."""
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            key = ".".join(parts[:i])
            if key not in sys.modules:
                m = types.ModuleType(key)
                sys.modules[key] = m
                if i > 1:
                    setattr(sys.modules[".".join(parts[:i - 1])], parts[i - 1], m)


_stub_module(
    "anthropic",
    "dotenv",
    "fastmcp",
    "fastmcp.utilities.types",
    "google.genai",
    "google.genai.types",
    "google.genai.errors",
    "google.auth.transport.requests",
    "google.oauth2.credentials",
    "google.oauth2.id_token",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery",
    "googleapiclient.http",
    "googleapiclient.errors",
    "apscheduler.schedulers.blocking",
    "apscheduler.executors.pool",
)

# google.genai stubs
class _GenAIAPIError(Exception):
    def __init__(self, code=500, response_json=None, *args, **kwargs):
        super().__init__(f"{code} {response_json}")
        self.code = code
        self.response_json = response_json

sys.modules["google.genai.errors"].APIError = _GenAIAPIError
sys.modules["google.genai.errors"].ClientError = type("ClientError", (_GenAIAPIError,), {})
sys.modules["google.genai.errors"].ServerError = type("ServerError", (_GenAIAPIError,), {})

class _GenerateContentConfig:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

sys.modules["google.genai.types"].GenerateContentConfig = _GenerateContentConfig
sys.modules["google.genai"].Client = object

# dotenv stub: load_dotenv() is a no-op in tests
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# fastmcp stub
class _FastMCP:
    def __init__(self, *a, **k): pass
    def tool(self): return lambda f: f

sys.modules["fastmcp"].FastMCP = _FastMCP

# fastmcp.utilities.types.Image stub — mirrors the real fastmcp.Image helper
# closely enough for classpilot.server to import and construct it; tests
# that care about real MCP-protocol image conversion use the real fastmcp
# package directly (see tests/test_visual_extractor.py), not this stub.
class _FakeImage:
    def __init__(self, path=None, data=None, format=None, annotations=None):
        self.path = path
        self.data = data
        self.format = format
        self.mimeType = f"image/{format}" if format else "image/png"

sys.modules["fastmcp.utilities.types"].Image = _FakeImage

# Google API stubs
sys.modules["google.auth.transport.requests"].Request = object
sys.modules["google.oauth2.credentials"].Credentials = object
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = object
sys.modules["google_auth_oauthlib.flow"].Flow = object
disc = sys.modules["googleapiclient.discovery"]
disc.build = lambda *a, **k: None
disc.Resource = object
sys.modules["googleapiclient.http"].MediaIoBaseUpload = object
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception

# APScheduler stubs
class _BlockingScheduler:
    def __init__(self, **k): pass
    def add_job(self, *a, **k): pass
    def start(self): pass

class _ThreadPoolExecutor:
    def __init__(self, **k): pass

sys.modules["apscheduler.schedulers.blocking"].BlockingScheduler = _BlockingScheduler
sys.modules["apscheduler.executors.pool"].ThreadPoolExecutor = _ThreadPoolExecutor

"""
tests/test_http_server.py  —  Streamable HTTP transport entry point

Verifies:
  1. Config picks up MCP_HTTP_HOST / MCP_HTTP_PORT / MCP_HTTP_PATH from env.
  2. Defaults are correct (127.0.0.1 : 8000 / /mcp).
  3. http_server.py imports the existing `mcp` instance — no new instance created.
  4. No tools are duplicated — tool count is identical between server.py imports.
  5. http_server.main() calls mcp.run() with the right transport and kwargs.
  6. All 120 existing tests still pass (run the other suites to verify).

No real uvicorn / HTTP server is started in these tests.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── stub every external dep before any classpilot import ─────────────────────
import types as _types, sys as _sys
_aps  = _types.ModuleType("apscheduler");           _aps.__path__  = []; _sys.modules["apscheduler"]            = _aps
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
    "anthropic", "dotenv", "fastmcp",
    "google.auth.transport.requests", "google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery", "googleapiclient.http", "googleapiclient.errors",
    "apscheduler.schedulers.blocking", "apscheduler.schedulers.background",
    "apscheduler.executors.pool",
    "pydantic",
)
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# Minimal FastMCP stub — separate instance per server name so vendor
# tools do not pollute the ClassPilot AI instance.
class _FakeMCP:
    def __init__(self, name="", **k):
        self.name = name
        self._tools = {}
        self.run_calls = []
    def tool(self, *a, **k):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator
    def run(self, transport=None, **kwargs):
        self.run_calls.append({"transport": transport, **kwargs})

_instances: dict = {}
def _mcp_factory(name="", **k):
    if name not in _instances:
        _instances[name] = _FakeMCP(name)
    return _instances[name]

_fake_mcp_instance = _mcp_factory("ClassPilot AI")
sys.modules["fastmcp"].FastMCP = _mcp_factory

sys.modules["google.auth.transport.requests"].Request = object
sys.modules["google.oauth2.credentials"].Credentials = object
sys.modules["google_auth_oauthlib.flow"].InstalledAppFlow = object
sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
sys.modules["googleapiclient.discovery"].Resource = object
sys.modules["googleapiclient.http"].MediaIoBaseUpload = object
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception
sys.modules["apscheduler.schedulers.blocking"].BlockingScheduler = type(
    "BS", (), {"__init__": lambda s, **k: None, "add_job": lambda s, *a, **k: None, "start": lambda s: None}
)
sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = type(
    "BGS", (), {"__init__": lambda s, **k: None, "add_job": lambda s, *a, **k: None,
                "start": lambda s: None, "shutdown": lambda s, **k: None,
                "remove_job": lambda s, j: None, "running": True}
)
sys.modules["apscheduler.executors.pool"].ThreadPoolExecutor = type(
    "TPE", (), {"__init__": lambda s, **k: None}
)

# Pydantic stub — BaseModel used in server.py for tool output models
class _BaseModel:
    def __init_subclass__(cls, **k): pass
    def __init__(self, **k):
        for key, val in k.items():
            setattr(self, key, val)
class _Field:
    def __call__(self, *a, **k): return None
    def __class_getitem__(cls, item): return cls

sys.modules["pydantic"].BaseModel = _BaseModel
sys.modules["pydantic"].Field = _Field()


# ── imports under test ────────────────────────────────────────────────────────
from classpilot.config import ClassPilotConfig, get_config


# ═════════════════════════════════════════════════════════════════════════════
class TestHttpConfig(unittest.TestCase):
    """Config correctly reads MCP_HTTP_* env vars with correct defaults."""

    def _fresh_config(self, env_overrides=None):
        """Build a ClassPilotConfig with optional env overrides, bypassing singleton."""
        env = {}
        if env_overrides:
            env.update(env_overrides)
        with patch.dict(os.environ, env, clear=False):
            return ClassPilotConfig()

    def test_default_host_is_localhost(self):
        cfg = self._fresh_config()
        self.assertEqual(cfg.mcp_http_host, "127.0.0.1")

    def test_default_port_is_8000(self):
        cfg = self._fresh_config()
        self.assertEqual(cfg.mcp_http_port, 8000)

    def test_default_path_is_slash_mcp(self):
        cfg = self._fresh_config()
        self.assertEqual(cfg.mcp_http_path, "/mcp")

    def test_host_override_from_env(self):
        cfg = self._fresh_config({"MCP_HTTP_HOST": "0.0.0.0"})
        self.assertEqual(cfg.mcp_http_host, "0.0.0.0")

    def test_port_override_from_env(self):
        cfg = self._fresh_config({"MCP_HTTP_PORT": "9090"})
        self.assertEqual(cfg.mcp_http_port, 9090)

    def test_path_override_from_env(self):
        cfg = self._fresh_config({"MCP_HTTP_PATH": "/classpilot/mcp"})
        self.assertEqual(cfg.mcp_http_path, "/classpilot/mcp")

    def test_port_is_int_not_string(self):
        cfg = self._fresh_config({"MCP_HTTP_PORT": "7777"})
        self.assertIsInstance(cfg.mcp_http_port, int)

    def test_http_config_coexists_with_other_config(self):
        """Adding HTTP fields must not break existing config fields."""
        cfg = self._fresh_config()
        self.assertIsNotNone(cfg.llm_provider)
        self.assertIsNotNone(cfg.watch_interval_minutes)
        self.assertIsNotNone(cfg.state_db_path)


# ═════════════════════════════════════════════════════════════════════════════
class TestHttpServerWiring(unittest.TestCase):
    """http_server.py must reuse the existing mcp instance — no new tools."""

    def test_http_server_imports_mcp_from_server_module(self):
        """http_server.mcp must be the same object as server.mcp."""
        import classpilot.server as srv
        import classpilot.http_server as http_srv
        self.assertIs(http_srv.mcp, srv.mcp)

    def test_no_new_tools_registered_by_http_server(self):
        """
        Importing http_server must not register any additional tools
        beyond what server.py already registered.
        """
        import classpilot.server as srv
        tools_before = set(srv.mcp._tools.keys())

        # Re-import http_server (may already be cached)
        import classpilot.http_server  # noqa: F401

        tools_after = set(srv.mcp._tools.keys())
        self.assertEqual(tools_before, tools_after,
                         "http_server.py must not register new tools")

    def test_eight_tools_registered_in_total(self):
        """Exactly 8 tools must be registered: 5 study + 3 watcher."""
        import classpilot.server as srv
        expected = {
            # Study tools
            "list_classes",
            "list_modules",
            "list_materials",
            "get_material",
            "search_classroom",
            # Watcher tools (kept)
            "get_upcoming_deadlines",
            "start_assignment_watcher",
            "stop_assignment_watcher",
        }
        self.assertEqual(set(srv.mcp._tools.keys()), expected)


# ═════════════════════════════════════════════════════════════════════════════
class TestHttpServerMain(unittest.TestCase):
    """http_server.main() calls mcp.run() with correct transport and kwargs."""

    def setUp(self):
        # Reset run_calls before each test
        _fake_mcp_instance.run_calls.clear()

    def _run_main(self, env_overrides=None):
        import classpilot.http_server as http_srv
        env = env_overrides or {}
        with patch.dict(os.environ, env, clear=False):
            # Patch get_config to return a fresh config with our overrides
            cfg = ClassPilotConfig()
            for k, v in env.items():
                if k == "MCP_HTTP_HOST": cfg.mcp_http_host = v
                if k == "MCP_HTTP_PORT": cfg.mcp_http_port = int(v)
                if k == "MCP_HTTP_PATH": cfg.mcp_http_path = v
            with patch("classpilot.http_server.get_config", return_value=cfg):
                with patch("classpilot.http_server.configure_logging"):
                    http_srv.main()

    def test_main_calls_mcp_run(self):
        self._run_main()
        self.assertEqual(len(_fake_mcp_instance.run_calls), 1)

    def test_transport_is_streamable_http(self):
        self._run_main()
        call = _fake_mcp_instance.run_calls[0]
        self.assertEqual(call["transport"], "streamable-http")

    def test_main_passes_default_host(self):
        self._run_main()
        self.assertEqual(_fake_mcp_instance.run_calls[0]["host"], "127.0.0.1")

    def test_main_passes_default_port(self):
        self._run_main()
        self.assertEqual(_fake_mcp_instance.run_calls[0]["port"], 8000)

    def test_main_passes_default_path(self):
        self._run_main()
        self.assertEqual(_fake_mcp_instance.run_calls[0]["path"], "/mcp")

    def test_main_passes_custom_host(self):
        self._run_main({"MCP_HTTP_HOST": "0.0.0.0"})
        self.assertEqual(_fake_mcp_instance.run_calls[0]["host"], "0.0.0.0")

    def test_main_passes_custom_port(self):
        self._run_main({"MCP_HTTP_PORT": "9090"})
        self.assertEqual(_fake_mcp_instance.run_calls[0]["port"], 9090)

    def test_main_passes_custom_path(self):
        self._run_main({"MCP_HTTP_PATH": "/api/mcp"})
        self.assertEqual(_fake_mcp_instance.run_calls[0]["path"], "/api/mcp")

    def test_stdio_transport_not_used(self):
        self._run_main()
        self.assertNotEqual(_fake_mcp_instance.run_calls[0]["transport"], "stdio")


# ═════════════════════════════════════════════════════════════════════════════
class TestNoCredentialExposure(unittest.TestCase):
    """Verify that sensitive config values are not exposed as MCP tools."""

    def test_no_tool_named_get_config(self):
        import classpilot.server as srv
        self.assertNotIn("get_config", srv.mcp._tools)

    def test_no_tool_named_get_credentials(self):
        import classpilot.server as srv
        self.assertNotIn("get_credentials", srv.mcp._tools)

    def test_no_tool_exposes_smtp_password(self):
        """None of the tool functions reference smtp_password."""
        import classpilot.server as srv
        import inspect
        for name, fn in srv.mcp._tools.items():
            src = inspect.getsource(fn)
            self.assertNotIn("smtp_password", src,
                             f"Tool '{name}' references smtp_password")

    def test_no_tool_exposes_api_key(self):
        import classpilot.server as srv
        import inspect
        for name, fn in srv.mcp._tools.items():
            src = inspect.getsource(fn)
            self.assertNotIn("llm_api_key", src,
                             f"Tool '{name}' references llm_api_key")


if __name__ == "__main__":
    unittest.main(verbosity=2)

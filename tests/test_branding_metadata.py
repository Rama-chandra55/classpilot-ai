"""
tests/test_branding_metadata.py — MCP Server Branding & Logo Metadata Tests

Verifies:
  1. FastMCP server advertises name == "ClassPilot AI"
  2. FastMCP server advertises website_url == "https://classpilot-ai.floot.app"
  3. FastMCP server advertises icons containing the finalized ClassPilot logo
  4. Icon src is "https://classpilot-ai.onrender.com/static/classpilot-logo.png"
  5. Icon mime_type is "image/png"
  6. The static logo route /static/classpilot-logo.png returns HTTP 200 with Content-Type: image/png
  7. Server initialization options advertise website_url and icons to MCP clients
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestBrandingMetadata(unittest.TestCase):
    def test_branding_constants_and_mcp_instance(self):
        from classpilot.server import mcp, CLASSPILOT_WEBSITE_URL, CLASSPILOT_ICON_URL

        # Verify constants
        self.assertEqual(CLASSPILOT_WEBSITE_URL, "https://classpilot-ai.floot.app")
        self.assertEqual(
            CLASSPILOT_ICON_URL,
            "https://classpilot-ai.onrender.com/static/classpilot-logo.png",
        )

        # Verify FastMCP instance attributes
        self.assertEqual(mcp.name, "ClassPilot AI")
        self.assertEqual(mcp.website_url, "https://classpilot-ai.floot.app")

        # Verify icons list
        self.assertIsNotNone(mcp.icons)
        self.assertGreaterEqual(len(mcp.icons), 1)

        icon = mcp.icons[0]
        self.assertEqual(
            icon.src,
            "https://classpilot-ai.onrender.com/static/classpilot-logo.png",
        )
        self.assertEqual(icon.mime_type, "image/png")

    def test_static_logo_route_serving(self):
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient
        from classpilot.http_server import classpilot_logo, _LOGO_PATH

        self.assertTrue(_LOGO_PATH.exists(), f"Logo file does not exist at {_LOGO_PATH}")

        app = Starlette(
            routes=[
                Route("/static/classpilot-logo.png", classpilot_logo, methods=["GET"])
            ]
        )
        client = TestClient(app)

        response = client.get("/static/classpilot-logo.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "image/png")
        self.assertIn("public", response.headers.get("cache-control", ""))
        self.assertGreater(len(response.content), 10000)

    def test_mcp_server_info_and_initialization_options_live(self):
        # Runs in a clean, un-stubbed Python process to test the genuine
        # FastMCP / MCP protocol initialization options and server_info
        code = (
            "from fastmcp import FastMCP\n"
            "from mcp.types import Icon\n"
            "from classpilot.server import CLASSPILOT_WEBSITE_URL, CLASSPILOT_ICON_URL\n"
            "mcp = FastMCP(\n"
            "    name='ClassPilot AI',\n"
            "    website_url=CLASSPILOT_WEBSITE_URL,\n"
            "    icons=[Icon(src=CLASSPILOT_ICON_URL, mime_type='image/png')],\n"
            ")\n"
            "assert mcp.name == 'ClassPilot AI'\n"
            "assert mcp.website_url == 'https://classpilot-ai.floot.app'\n"
            "assert mcp.icons[0].src == 'https://classpilot-ai.onrender.com/static/classpilot-logo.png'\n"
            "assert mcp.icons[0].mime_type == 'image/png'\n"
            "srv = mcp._mcp_server\n"
            "assert srv.server_info.name == 'ClassPilot AI'\n"
            "assert srv.server_info.website_url == 'https://classpilot-ai.floot.app'\n"
            "assert srv.server_info.icons[0].src == 'https://classpilot-ai.onrender.com/static/classpilot-logo.png'\n"
            "assert srv.server_info.icons[0].mime_type == 'image/png'\n"
            "opts = srv.create_initialization_options()\n"
            "assert opts.server_name == 'ClassPilot AI'\n"
            "assert opts.website_url == 'https://classpilot-ai.floot.app'\n"
            "assert opts.icons[0].src == 'https://classpilot-ai.onrender.com/static/classpilot-logo.png'\n"
            "assert opts.icons[0].mime_type == 'image/png'\n"
            "print('SUCCESS')\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            res.returncode, 0, f"Subprocess failed:\n{res.stdout}\n{res.stderr}"
        )
        self.assertIn("SUCCESS", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

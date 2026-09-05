"""
tests/test_get_material_visuals.py

Verifies classpilot.server.get_material's new mixed-content return shape:
[MaterialContent, *Image, ...] — added so diagrams/charts extracted by
visual_extractor.py actually reach the connected AI as real image content,
not just JSON metadata about them.

Self-contained stub setup (mirrors tests/test_http_server.py's pattern) so
this file doesn't depend on — or interfere with — the cross-file FastMCP
stub juggling the rest of the suite already has to do (see the comment on
_ensure_fake_mcp_active in test_http_server.py for why that's necessary).
"""

import io
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

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


_stub(
    "anthropic", "dotenv", "fastmcp", "fastmcp.utilities.types", "pydantic",
    "google.auth.transport.requests", "google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery", "googleapiclient.http", "googleapiclient.errors",
    "apscheduler.schedulers.blocking", "apscheduler.schedulers.background",
    "apscheduler.executors.pool",
)
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None


class _FakeMCP:
    def __init__(self, name="", **k):
        self.name = name
        self._tools = {}
    def tool(self, *a, **k):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator
    def run(self, transport=None, **kwargs):
        pass

sys.modules["fastmcp"].FastMCP = _FakeMCP


class _FakeImage:
    """Mirrors fastmcp.utilities.types.Image closely enough to test that
    server.py constructs images with the right data/format — the real
    conversion to MCP ImageContent is fastmcp's own, already verified
    manually against the real package during development of this feature."""
    def __init__(self, path=None, data=None, format=None, annotations=None):
        self.path = path
        self.data = data
        self.format = format

sys.modules["fastmcp.utilities.types"].Image = _FakeImage


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

for _mod in ("classpilot.server", "classpilot.classroom_client", "classpilot.study_client"):
    sys.modules.pop(_mod, None)

import classpilot.server as srv  # noqa: E402


def _make_png_bytes(size=(300, 300), color=(10, 20, 30)) -> bytes:
    from PIL import Image as PILImage
    im = PILImage.new("RGB", size, color=color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class TestGetMaterialReturnsMixedContent(unittest.TestCase):
    """get_material must return [MaterialContent, *Image] so a
    vision-capable connected AI actually receives the image bytes, not
    just a text description that an image exists."""

    def setUp(self):
        self.material = {
            "id": "m1", "kind": "MATERIAL", "title": "SVM.pptx", "description": "",
            "topic_id": "1", "state": "PUBLISHED", "due_date": None, "due_time": None,
            "link": "http://x", "created": "",
            "attachments": [{"type": "drive", "id": "f1", "title": "SVM.pptx", "link": "http://drive/f1"}],
        }

    def test_material_with_one_visual_returns_material_plus_one_image(self):
        fake_result = {
            "readable": True, "content": "--- Slide 1 ---\nIntro to SVM",
            "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "name": "SVM.pptx", "link": "http://drive/f1", "note": "",
            "visuals": [{
                "mime_type": "image/png", "data": _make_png_bytes(),
                "location": "Slide 5", "caption": "Slide 5 — diagram (no other text)",
                "diagram_only": True, "area_ratio": 0.4,
            }],
        }
        with patch("classpilot.server.fetch_course_work_materials", return_value=[self.material]), \
             patch("classpilot.server.read_drive_file", return_value=fake_result):
            result = srv.get_material(course_id="c1", material_id="m1")

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        material_content, image = result
        self.assertEqual(material_content.id, "m1")
        self.assertEqual(len(material_content.visuals), 1)
        self.assertEqual(material_content.visuals[0].location, "Slide 5")
        self.assertTrue(material_content.visuals[0].diagram_only)
        self.assertIsInstance(image, _FakeImage)
        self.assertEqual(image.format, "png")
        self.assertEqual(len(image.data), len(fake_result["visuals"][0]["data"]))

    def test_material_with_no_visuals_returns_single_item_list(self):
        fake_result = {
            "readable": True, "content": "Just text, no diagrams",
            "mime_type": "application/pdf", "name": "Notes.pdf",
            "link": "http://drive/f1", "note": "", "visuals": [],
        }
        with patch("classpilot.server.fetch_course_work_materials", return_value=[self.material]), \
             patch("classpilot.server.read_drive_file", return_value=fake_result):
            result = srv.get_material(course_id="c1", material_id="m1")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].visuals, [])

    def test_multiple_visuals_produce_multiple_images_in_order(self):
        fake_result = {
            "readable": True, "content": "text",
            "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "name": "Deck.pptx", "link": "http://drive/f1", "note": "",
            "visuals": [
                {"mime_type": "image/png", "data": _make_png_bytes((100, 100), (1, 1, 1)),
                 "location": "Slide 5", "caption": "c5", "diagram_only": True, "area_ratio": 0.3},
                {"mime_type": "image/jpeg", "data": _make_png_bytes((100, 100), (2, 2, 2)),
                 "location": "Slide 7", "caption": "c7", "diagram_only": True, "area_ratio": 0.35},
            ],
        }
        with patch("classpilot.server.fetch_course_work_materials", return_value=[self.material]), \
             patch("classpilot.server.read_drive_file", return_value=fake_result):
            result = srv.get_material(course_id="c1", material_id="m1")

        self.assertEqual(len(result), 3)
        self.assertEqual(result[1].format, "png")
        self.assertEqual(result[2].format, "jpeg")
        self.assertEqual([v.location for v in result[0].visuals], ["Slide 5", "Slide 7"])

    def test_visuals_capped_across_multiple_attachments(self):
        """The per-response cap (_MAX_VISUALS_PER_RESPONSE) must hold even
        if several attachments each contribute images."""
        material = dict(self.material, attachments=[
            {"type": "drive", "id": "f1", "title": "A.pptx", "link": "http://a"},
            {"type": "drive", "id": "f2", "title": "B.pptx", "link": "http://b"},
        ])
        many_visuals = [
            {"mime_type": "image/png", "data": _make_png_bytes((50, 50), (i, i, i)),
             "location": f"Slide {i}", "caption": f"c{i}", "diagram_only": True, "area_ratio": 0.3}
            for i in range(6)
        ]
        fake_result = {
            "readable": True, "content": "text", "mime_type": "application/pdf",
            "name": "X", "link": "http://x", "note": "", "visuals": many_visuals,
        }
        with patch("classpilot.server.fetch_course_work_materials", return_value=[material]), \
             patch("classpilot.server.read_drive_file", return_value=fake_result):
            result = srv.get_material(course_id="c1", material_id="m1")

        images = result[1:]
        self.assertLessEqual(len(images), srv._MAX_VISUALS_PER_RESPONSE)

    def test_read_attachments_false_skips_visuals_entirely(self):
        with patch("classpilot.server.fetch_course_work_materials", return_value=[self.material]), \
             patch("classpilot.server.read_drive_file") as mock_read:
            result = srv.get_material(course_id="c1", material_id="m1", read_attachments=False)

        mock_read.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].visuals, [])


class TestImageFormatMapping(unittest.TestCase):
    def test_png(self):
        self.assertEqual(srv._image_format_from_mime("image/png"), "png")

    def test_jpeg_variants(self):
        self.assertEqual(srv._image_format_from_mime("image/jpeg"), "jpeg")
        self.assertEqual(srv._image_format_from_mime("image/jpg"), "jpeg")

    def test_unknown_mime_defaults_to_png(self):
        self.assertEqual(srv._image_format_from_mime("application/octet-stream"), "png")

    def test_case_insensitive(self):
        self.assertEqual(srv._image_format_from_mime("IMAGE/PNG"), "png")


if __name__ == "__main__":
    unittest.main()

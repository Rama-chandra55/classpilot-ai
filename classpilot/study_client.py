"""
Study Client

Raw Google API calls used by the 5 ClassPilot Study Assistant tools.
No business logic, no MCP concerns — just data retrieval.

Content-reading capabilities (honest):
  Google Docs       → full text via Docs API               ✅
  Google Slides     → full text via Drive export (text/plain) ✅
  Google Sheets     → CSV via Drive export                 ✅
  Plain text files  → raw content via Drive download       ✅
  PowerPoint .pptx  → full slide text via python-pptx      ✅
  PDF (uploaded)    → full text via pypdf                  ✅
  Word .docx        → full text via python-docx            ✅
  PNG/JPEG images   → handed to the AI as visual content    ✅ (see below)
  Legacy .ppt/.doc  → URL only, no text extraction         ⚠️
  YouTube / links   → URL only                             ⚠️

Visual content (diagrams/charts/screenshots — NOT OCR):
  PPTX/PDF/DOCX materials often contain diagram-only slides/pages that have
  no extractable text at all (e.g. an architecture diagram with no title).
  read_drive_file() additionally pulls out important embedded images via
  visual_extractor.py and returns them under "visuals" so the connected AI
  can look at them directly with its own vision, the same way a human
  would. This is separate from and does not change the text extraction
  above in any way — see visual_extractor.py for the filtering approach
  (size + repeated-logo/watermark filtering, capped per file).

All functions raise exceptions on failure; callers catch and surface errors.
"""

import io
import logging
from typing import Any, Optional

from .classroom_client import classroom, drive, docs
from .visual_extractor import (
    extract_pptx_visuals,
    extract_pdf_visuals,
    extract_docx_visuals,
    prepare_standalone_image,
)
from classroom_suite_mcp.auth import get_classroom_service, get_drive_service, get_docs_service
from googleapiclient.http import MediaIoBaseDownload
from pptx import Presentation
from pypdf import PdfReader
from docx import Document as DocxDocument

logger = logging.getLogger(__name__)

# ── MIME type routing ─────────────────────────────────────────────────────────

# Google Workspace types that can be exported to readable text
_GOOGLE_TEXT_EXPORTS = {
    "application/vnd.google-apps.document":     "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet":  "text/csv",
}

# Native text MIME types we can download directly
_NATIVE_TEXT_TYPES = {
    "text/plain", "text/markdown", "text/html", "text/csv",
    "application/json", "application/xml",
}

# Uploaded .pptx (OOXML) — downloaded and parsed with python-pptx.
# Legacy binary .ppt (application/vnd.ms-powerpoint) is NOT supported by
# python-pptx and stays in _URL_ONLY_TYPES below.
_PPTX_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# Uploaded .pdf — downloaded and parsed with pypdf.
_PDF_TYPES = {
    "application/pdf",
}

# Uploaded .docx (OOXML) — downloaded and parsed with python-docx.
# Legacy binary .doc is NOT supported by python-docx and stays in
# _URL_ONLY_TYPES below.
_DOCX_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Uploaded standalone image files — downloaded directly and handed to the
# connected AI as visual content (no OCR, no filtering: the whole file IS
# the material). Diagram/chart images *embedded inside* PPTX/PDF/DOCX are
# handled separately by visual_extractor.py.
_IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/jpg",
}

# Types we cannot read — return URL only
_URL_ONLY_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/msword",
    "image/gif", "image/webp",
    "video/mp4", "audio/mpeg",
}


# ── Pagination helper ──────────────────────────────────────────────────────────

def _paginate(list_fn, item_key: str, params: dict) -> list[dict]:
    """
    Drive a Google Classroom `.list()` endpoint to exhaustion.

    Classroom list endpoints (courses, topics, courseWorkMaterials, courseWork,
    announcements, ...) cap each response to a single page and return a
    `nextPageToken` when more results exist. Calling `.list().execute()` once
    silently truncates to that first page. This helper repeatedly calls
    `list_fn(**params, pageToken=...)` and follows `nextPageToken` until the
    API stops returning one, accumulating every item under `item_key` across
    all pages.

    Args:
        list_fn: bound `.list` method, e.g. `svc.courses().list`.
        item_key: the response key holding this page's items
                   (e.g. "courses", "topic", "courseWork").
        params: base query params (courseId, courseStates, pageSize, ...),
                without pageToken — pageToken is added automatically.

    Returns:
        All items across every page, in the order the API returned them.
    """
    items: list[dict] = []
    page_token: Optional[str] = None
    while True:
        call_params = dict(params)
        if page_token:
            call_params["pageToken"] = page_token
        result = list_fn(**call_params).execute()
        items.extend(result.get(item_key) or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items


# ── 1. Courses ────────────────────────────────────────────────────────────────

def fetch_courses(page_size: int = 100) -> list[dict[str, Any]]:
    """List all active Classroom courses the student is enrolled in.

    Pages through the full result set — courses are not silently truncated
    if the student is enrolled in more than one page's worth.
    """
    svc = get_classroom_service()
    raw = _paginate(
        svc.courses().list,
        "courses",
        {"courseStates": ["ACTIVE"], "pageSize": page_size},
    )
    return [
        {
            "id":          c.get("id"),
            "name":        c.get("name", "Unnamed"),
            "section":     c.get("section"),
            "description": c.get("descriptionHeading"),
            "room":        c.get("room"),
            "state":       c.get("courseState"),
            "link":        c.get("alternateLink"),
        }
        for c in raw
    ]


# ── 2. Topics / Modules ───────────────────────────────────────────────────────

def fetch_topics(course_id: str, page_size: int = 100) -> list[dict[str, Any]]:
    """List all topics (modules) in a course, ordered by topicId.

    Pages through the full result set — courses with many modules will not
    have later topics silently dropped.
    """
    svc = get_classroom_service()
    raw = _paginate(
        svc.courses().topics().list,
        "topic",
        {"courseId": course_id, "pageSize": page_size},
    )
    # Classroom API returns topics with updateTime but no explicit order field;
    # topicId is an incrementing integer — sort ascending to get creation order.
    sorted_topics = sorted(raw, key=lambda t: int(t.get("topicId", "0")))
    return [
        {
            "id":           t.get("topicId"),
            "name":         t.get("name", "Unnamed"),
            "updated_time": t.get("updateTime"),
        }
        for t in sorted_topics
    ]


# ── 3. Materials in a module ──────────────────────────────────────────────────

def fetch_course_work_materials(course_id: str, topic_id: Optional[str] = None) -> list[dict]:
    """
    Fetch teacher-posted study materials (courseWorkMaterial resources).
    Requires the classroom.courseworkmaterials scope.

    Pages through the full result set before filtering by topic, so modules
    beyond the first page of materials are not silently dropped.
    """
    svc = get_classroom_service()
    items = _paginate(
        svc.courses().courseWorkMaterials().list,
        "courseWorkMaterial",
        {"courseId": course_id, "pageSize": 100},
    )
    if topic_id:
        items = [i for i in items if i.get("topicId") == topic_id]
    return [_normalise_material(m, "MATERIAL") for m in items]


def fetch_assignments(course_id: str, topic_id: Optional[str] = None) -> list[dict]:
    """
    Fetch assignments for a course/topic.
    Shown as info only — no submission capability exposed.

    Pages through the full result set before filtering by topic.
    """
    svc = get_classroom_service()
    items = _paginate(
        svc.courses().courseWork().list,
        "courseWork",
        {"courseId": course_id, "pageSize": 100},
    )
    if topic_id:
        items = [i for i in items if i.get("topicId") == topic_id]
    return [_normalise_material(m, "ASSIGNMENT") for m in items]


def fetch_announcements(course_id: str) -> list[dict]:
    """Fetch course announcements. Requires classroom.announcements scope.

    Pages through the full result set so older announcements beyond the
    first page are still returned.
    """
    svc = get_classroom_service()
    try:
        items = _paginate(
            svc.courses().announcements().list,
            "announcement",
            {"courseId": course_id, "pageSize": 100},
        )
        return [_normalise_material(a, "ANNOUNCEMENT") for a in items]
    except Exception as exc:
        logger.warning("Could not fetch announcements for %s: %s", course_id, exc)
        return []


def _normalise_material(raw: dict, kind: str) -> dict:
    """Normalise a Classroom API resource into a consistent shape."""
    materials = raw.get("materials", [])
    attachments = []
    for m in materials:
        if "driveFile" in m:
            df = m["driveFile"].get("driveFile", m["driveFile"])
            attachments.append({
                "type":     "drive",
                "id":       df.get("id"),
                "title":    df.get("title") or df.get("name"),
                "link":     df.get("alternateLink"),
            })
        elif "youtubeVideo" in m:
            yt = m["youtubeVideo"]
            attachments.append({
                "type":  "youtube",
                "id":    yt.get("id"),
                "title": yt.get("title"),
                "link":  yt.get("alternateLink"),
            })
        elif "link" in m:
            lk = m["link"]
            attachments.append({
                "type":  "link",
                "title": lk.get("title") or lk.get("url"),
                "link":  lk.get("url"),
            })
        elif "form" in m:
            fm = m["form"]
            attachments.append({
                "type":  "form",
                "title": fm.get("title"),
                "link":  fm.get("formUrl"),
            })

    return {
        "id":          raw.get("id"),
        "kind":        kind,                          # MATERIAL | ASSIGNMENT | ANNOUNCEMENT
        "title":       raw.get("title") or raw.get("text", "")[:80],
        "description": raw.get("description") or raw.get("text", ""),
        "topic_id":    raw.get("topicId"),
        "state":       raw.get("state"),
        "due_date":    raw.get("dueDate"),
        "due_time":    raw.get("dueTime"),
        "link":        raw.get("alternateLink"),
        "created":     raw.get("creationTime"),
        "attachments": attachments,
    }


# ── 4. File content extraction ────────────────────────────────────────────────

def read_drive_file(file_id: str) -> dict[str, Any]:
    """
    Attempt to read the text content of a Drive file, and — for
    PPTX/PDF/DOCX and direct image attachments — pull out any important
    embedded images/diagrams/charts as well.

    Returns a dict with:
      readable  bool   — True if text content was extracted
      content   str    — extracted text (or empty string)
      mime_type str    — the file's MIME type
      name      str    — filename
      link      str    — webViewLink for the user to open manually
      note      str    — human-readable explanation when not readable
      visuals   list   — important images for the connected AI to look at
                          directly (NOT OCR — raw image bytes only). Each
                          item: {mime_type, data, location, caption,
                          diagram_only, area_ratio}. Empty for file types
                          with no visual pipeline, or when a file has no
                          images meeting the significance threshold.
    """
    svc = get_drive_service()

    # Get file metadata first
    meta = svc.files().get(
        fileId=file_id,
        fields="id,name,mimeType,webViewLink,size"
    ).execute()

    mime = meta.get("mimeType", "")
    name = meta.get("name", "")
    link = meta.get("webViewLink", "")

    # ── Google Workspace types: export to text ────────────────────────────────
    if mime in _GOOGLE_TEXT_EXPORTS:
        export_mime = _GOOGLE_TEXT_EXPORTS[mime]
        try:
            request = svc.files().export_media(fileId=file_id, mimeType=export_mime)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            text = buf.getvalue().decode("utf-8", errors="replace").strip()
            readable = bool(text)
            return {
                "readable":  readable,
                "content":   text if readable else "",
                "mime_type": mime,
                "name":      name,
                "link":      link,
                "note":      "" if readable else "File exported but contained no text.",
                "visuals":   [],
            }
        except Exception as exc:
            logger.warning("Export failed for %s (%s): %s", name, mime, exc)
            return _url_only(name, mime, link, f"Export failed: {exc}")

    # ── Uploaded .pptx: download raw bytes and parse with python-pptx ─────────
    if mime in _PPTX_TYPES:
        try:
            request = svc.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            text = _extract_pptx_text(buf)
            readable = bool(text)
            return {
                "readable":  readable,
                "content":   text if readable else "",
                "mime_type": mime,
                "name":      name,
                "link":      link,
                "note":      "" if readable else "PPTX downloaded but no extractable text was found (slides may be image-only).",
                "visuals":   _safe_extract_visuals(extract_pptx_visuals, buf, name),
            }
        except Exception as exc:
            logger.warning("PPTX extraction failed for %s: %s", name, exc)
            return _url_only(
                name, mime, link,
                f"Could not read PowerPoint content ({exc}). Open the link to view it.",
            )

    # ── Uploaded .pdf: download raw bytes and parse with pypdf ────────────────
    if mime in _PDF_TYPES:
        try:
            request = svc.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            text = _extract_pdf_text(buf)
            readable = bool(text)
            return {
                "readable":  readable,
                "content":   text if readable else "",
                "mime_type": mime,
                "name":      name,
                "link":      link,
                "note":      "" if readable else "PDF downloaded but no extractable text was found (it may be scanned/image-only).",
                "visuals":   _safe_extract_visuals(extract_pdf_visuals, buf, name),
            }
        except Exception as exc:
            logger.warning("PDF extraction failed for %s: %s", name, exc)
            return _url_only(
                name, mime, link,
                f"Could not read PDF content ({exc}). Open the link to view it.",
            )

    # ── Uploaded .docx: download raw bytes and parse with python-docx ─────────
    if mime in _DOCX_TYPES:
        try:
            request = svc.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            text = _extract_docx_text(buf)
            readable = bool(text)
            return {
                "readable":  readable,
                "content":   text if readable else "",
                "mime_type": mime,
                "name":      name,
                "link":      link,
                "note":      "" if readable else "DOCX downloaded but no extractable text was found.",
                "visuals":   _safe_extract_visuals(extract_docx_visuals, buf, name),
            }
        except Exception as exc:
            logger.warning("DOCX extraction failed for %s: %s", name, exc)
            return _url_only(
                name, mime, link,
                f"Could not read Word document content ({exc}). Open the link to view it.",
            )

    # ── Uploaded image (PNG/JPEG): download raw bytes, no text/OCR ────────────
    # The whole file IS the material here, so it's handed over as-is —
    # no size/repetition filtering (that's only needed for images embedded
    # among many shapes in a slide/page/document).
    if mime in _IMAGE_TYPES:
        try:
            request = svc.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            data = buf.getvalue()
            return {
                "readable":  False,   # no text content — "visuals" is what matters here
                "content":   "",
                "mime_type": mime,
                "name":      name,
                "link":      link,
                "note":      "Image file — no text content. See 'visuals' for the image itself.",
                "visuals":   prepare_standalone_image(data, mime, name),
            }
        except Exception as exc:
            logger.warning("Image download failed for %s: %s", name, exc)
            return _url_only(name, mime, link, f"Could not download image ({exc}). Open the link to view it.")

    # ── Native text types: download directly ─────────────────────────────────
    if mime in _NATIVE_TEXT_TYPES:
        try:
            request = svc.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
            text = buf.getvalue().decode("utf-8", errors="replace").strip()
            return {
                "readable":  True,
                "content":   text,
                "mime_type": mime,
                "name":      name,
                "link":      link,
                "note":      "",
                "visuals":   [],
            }
        except Exception as exc:
            logger.warning("Download failed for %s: %s", name, exc)
            return _url_only(name, mime, link, f"Download failed: {exc}")

    # ── Everything else: URL only ─────────────────────────────────────────────
    note = _url_only_note(mime)
    return _url_only(name, mime, link, note)


def _extract_pptx_text(buf: io.BytesIO) -> str:
    """
    Extract readable text from a .pptx file already downloaded into `buf`.

    Walks every slide and pulls text from:
      - text boxes / placeholders (titles, bullets, body text)
      - tables
      - speaker notes

    Returns the concatenated text, one section per slide. Returns "" if the
    deck has no extractable text (e.g. image-only slides).
    """
    buf.seek(0)
    prs = Presentation(buf)
    slide_sections: list[str] = []

    for idx, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []

        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        lines.append(line)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        lines.append(" | ".join(cells))

        if getattr(slide, "has_notes_slide", False) and slide.notes_slide is not None:
            notes_frame = slide.notes_slide.notes_text_frame
            notes = notes_frame.text.strip() if notes_frame else ""
            if notes:
                lines.append(f"[Speaker notes: {notes}]")

        if lines:
            slide_sections.append(f"--- Slide {idx} ---\n" + "\n".join(lines))

    return "\n\n".join(slide_sections).strip()


def _extract_pdf_text(buf: io.BytesIO) -> str:
    """
    Extract readable text from a .pdf file already downloaded into `buf`.

    Walks every page and pulls its extracted text. Returns the concatenated
    text, one section per page. Returns "" if the PDF has no extractable
    text (e.g. scanned/image-only pages) or if the file is corrupted/empty.
    """
    buf.seek(0)
    reader = PdfReader(buf)
    page_sections: list[str] = []

    for idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            page_sections.append(f"--- Page {idx} ---\n{text}")

    return "\n\n".join(page_sections).strip()


def _extract_docx_text(buf: io.BytesIO) -> str:
    """
    Extract readable text from a .docx file already downloaded into `buf`.

    Walks the document and pulls text from:
      - paragraphs (headings, body text)
      - tables

    Returns the concatenated text. Returns "" if the document has no
    extractable text.
    """
    buf.seek(0)
    doc = DocxDocument(buf)
    lines: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            lines.append(text)

    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))

    return "\n".join(lines).strip()


def _safe_extract_visuals(extractor, buf: io.BytesIO, name: str) -> list[dict]:
    """
    Run a visual extractor and never let it break text extraction.

    Visual extraction is a strictly additive feature — if it raises for
    any reason (a malformed embedded image, an unexpected shape structure,
    etc.), the material's text content (already extracted successfully at
    this point) must still be returned untouched. Only the image list is
    lost, silently, with a log line for diagnosis.
    """
    try:
        return extractor(buf)
    except Exception as exc:
        logger.warning("Visual extraction failed for %s: %s", name, exc)
        return []


def _url_only(name: str, mime: str, link: str, note: str) -> dict:
    return {
        "readable":  False,
        "content":   "",
        "mime_type": mime,
        "name":      name,
        "link":      link,
        "note":      note,
        "visuals":   [],
    }


def _url_only_note(mime: str) -> str:
    if mime == "application/msword":
        return "This is a legacy .doc file, which ClassPilot cannot parse. Modern .docx uploads are read automatically — open the link to view this one."
    if "powerpoint" in mime:
        return "This is a legacy .ppt file, which ClassPilot cannot parse. Modern .pptx uploads are read automatically — open the link to view this one."
    if "image" in mime:
        return "This is an image file. Open the link to view it."
    if "video" in mime or "audio" in mime:
        return "This is a media file. Open the link to play it."
    return "This file type cannot be read as text. Open the link to access it."


# ── 5. Search across Classroom ────────────────────────────────────────────────

def search_all(query: str) -> list[dict[str, Any]]:
    """
    Search course names, topic names, and material titles for a query string.
    Case-insensitive substring match. Returns results with location context.
    """
    q = query.lower().strip()
    results = []

    try:
        courses = fetch_courses()
    except Exception as exc:
        logger.error("search_all: failed to list courses: %s", exc)
        return []

    for course in courses:
        cid   = course["id"]
        cname = course["name"]

        # Course name match
        if q in cname.lower():
            results.append({
                "type":        "course",
                "course_name": cname,
                "title":       cname,
                "link":        course.get("link"),
                "context":     "Course",
            })

        # Topic matches
        try:
            topics = fetch_topics(cid)
        except Exception:
            topics = []

        for topic in topics:
            if q in topic["name"].lower():
                results.append({
                    "type":        "module",
                    "course_name": cname,
                    "topic_id":    topic["id"],
                    "title":       topic["name"],
                    "context":     f"{cname} → Module",
                })

        # Material / assignment title matches
        for fetcher in (fetch_course_work_materials, fetch_assignments):
            try:
                items = fetcher(cid)
            except Exception:
                items = []
            for item in items:
                title = item.get("title", "")
                desc  = item.get("description", "")
                if q in title.lower() or q in desc.lower():
                    topic_name = next(
                        (t["name"] for t in topics if t["id"] == item.get("topic_id")),
                        "No module"
                    )
                    results.append({
                        "type":        "material",
                        "kind":        item["kind"],
                        "course_name": cname,
                        "topic_name":  topic_name,
                        "title":       title,
                        "link":        item.get("link"),
                        "context":     f"{cname} → {topic_name}",
                    })

    return results

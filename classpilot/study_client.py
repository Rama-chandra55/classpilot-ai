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
  PDF (uploaded)    → URL only, no text extraction         ⚠️
  Legacy .ppt       → URL only, no text extraction         ⚠️
  YouTube / links   → URL only                             ⚠️

All functions raise exceptions on failure; callers catch and surface errors.
"""

import io
import logging
from typing import Any, Optional

from .classroom_client import classroom, drive, docs
from classroom_suite_mcp.auth import get_classroom_service, get_drive_service, get_docs_service
from googleapiclient.http import MediaIoBaseDownload
from pptx import Presentation

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

# Types we cannot read — return URL only
_URL_ONLY_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "video/mp4", "audio/mpeg",
}


# ── 1. Courses ────────────────────────────────────────────────────────────────

def fetch_courses(page_size: int = 50) -> list[dict[str, Any]]:
    """List all active Classroom courses the student is enrolled in."""
    svc = get_classroom_service()
    result = svc.courses().list(
        courseStates=["ACTIVE"],
        pageSize=page_size,
    ).execute()
    raw = result.get("courses", [])
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

def fetch_topics(course_id: str) -> list[dict[str, Any]]:
    """List all topics (modules) in a course, ordered by topicId."""
    svc = get_classroom_service()
    result = svc.courses().topics().list(courseId=course_id).execute()
    raw = result.get("topic", [])
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
    """
    svc = get_classroom_service()
    params: dict = {"courseId": course_id, "pageSize": 100}
    result = svc.courses().courseWorkMaterials().list(**params).execute()
    items = result.get("courseWorkMaterial", [])
    if topic_id:
        items = [i for i in items if i.get("topicId") == topic_id]
    return [_normalise_material(m, "MATERIAL") for m in items]


def fetch_assignments(course_id: str, topic_id: Optional[str] = None) -> list[dict]:
    """
    Fetch assignments for a course/topic.
    Shown as info only — no submission capability exposed.
    """
    svc = get_classroom_service()
    result = svc.courses().courseWork().list(
        courseId=course_id, pageSize=100
    ).execute()
    items = result.get("courseWork", [])
    if topic_id:
        items = [i for i in items if i.get("topicId") == topic_id]
    return [_normalise_material(m, "ASSIGNMENT") for m in items]


def fetch_announcements(course_id: str) -> list[dict]:
    """Fetch course announcements. Requires classroom.announcements scope."""
    svc = get_classroom_service()
    try:
        result = svc.courses().announcements().list(
            courseId=course_id, pageSize=50
        ).execute()
        items = result.get("announcement", [])
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
    Attempt to read the text content of a Drive file.

    Returns a dict with:
      readable  bool   — True if text content was extracted
      content   str    — extracted text (or empty string)
      mime_type str    — the file's MIME type
      name      str    — filename
      link      str    — webViewLink for the user to open manually
      note      str    — human-readable explanation when not readable
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
            }
        except Exception as exc:
            logger.warning("PPTX extraction failed for %s: %s", name, exc)
            return _url_only(
                name, mime, link,
                f"Could not read PowerPoint content ({exc}). Open the link to view it.",
            )

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


def _url_only(name: str, mime: str, link: str, note: str) -> dict:
    return {
        "readable":  False,
        "content":   "",
        "mime_type": mime,
        "name":      name,
        "link":      link,
        "note":      note,
    }


def _url_only_note(mime: str) -> str:
    if "pdf" in mime:
        return "This is a PDF file. Text extraction requires an additional library. Open the link to read it."
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

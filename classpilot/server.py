"""
ClassPilot AI — MCP Server

Study Assistant + Classroom Watcher in one FastMCP server.

Study tools (NEW):
    list_classes          — list all enrolled courses
    list_modules          — list topics/modules in a course
    list_materials        — list all materials in a module
    get_material          — retrieve and read a material's content
    search_classroom      — search across courses, modules, materials

Watcher tools (kept):
    get_upcoming_deadlines  — deadlines sorted by proximity
    start_assignment_watcher — begin background polling + notifications
    stop_assignment_watcher  — stop the watcher

Transport: stdio (Claude Desktop, Cursor) or HTTP (classpilot/http_server.py)
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from .services import AppServices, build_services
from .deadline_scheduler import _parse_due_datetime
from .study_client import (
    fetch_courses,
    fetch_topics,
    fetch_course_work_materials,
    fetch_assignments,
    fetch_announcements,
    read_drive_file,
    search_all,
)

logger = logging.getLogger(__name__)

# ── FastMCP instance ──────────────────────────────────────────────────────────

mcp = FastMCP(
    name="ClassPilot AI",
    instructions=(
        "ClassPilot AI is your Google Classroom study assistant. "
        "Navigate: list_classes → list_modules → list_materials → get_material. "
        "Use search_classroom when you don't know the exact location. "
        "Use get_upcoming_deadlines to see what's due soon. "
        "The assistant reads Google Docs and Slides content directly so you "
        "can ask questions about course materials without opening them manually."
    ),
)

# ── Shared services (lazy-init) ───────────────────────────────────────────────

_services: Optional[AppServices] = None


def _get_services() -> AppServices:
    global _services
    if _services is None:
        _services = build_services(validate=False)
    return _services


# ── Pydantic output models ────────────────────────────────────────────────────

class CourseInfo(BaseModel):
    id: str
    name: str
    section: Optional[str] = None
    description: Optional[str] = None
    room: Optional[str] = None
    link: Optional[str] = None


class ModuleInfo(BaseModel):
    id: str
    name: str
    updated_time: Optional[str] = None


class AttachmentInfo(BaseModel):
    type: str = Field(description="drive | youtube | link | form")
    title: Optional[str] = None
    link: Optional[str] = None
    id: Optional[str] = None


class MaterialInfo(BaseModel):
    id: str
    kind: str = Field(description="MATERIAL | ASSIGNMENT | ANNOUNCEMENT")
    title: str
    description: Optional[str] = None
    state: Optional[str] = None
    due_date: Optional[str] = None
    attachments: List[AttachmentInfo] = []
    link: Optional[str] = None


class MaterialContent(BaseModel):
    id: str
    title: str
    kind: str
    description: Optional[str] = None
    attachments: List[dict] = []
    link: Optional[str] = None


class FileContent(BaseModel):
    name: str
    readable: bool
    content: str = Field(description="Extracted text content, or empty string if not readable")
    mime_type: str
    link: str
    note: str = Field(description="Explains any limitation when content could not be read")


class SearchResult(BaseModel):
    type: str = Field(description="course | module | material")
    title: str
    context: str = Field(description="e.g. 'Quantum Computing → Module 1'")
    course_name: Optional[str] = None
    topic_name: Optional[str] = None
    kind: Optional[str] = None
    link: Optional[str] = None


class DeadlineInfo(BaseModel):
    course_name: str
    title: str
    due_date: str
    due_time_utc: str
    minutes_remaining: int
    hours_remaining: float
    state: str


class WatcherResult(BaseModel):
    running: bool
    message: str


# ── Helper: format due date ───────────────────────────────────────────────────

def _fmt_due(due_date: Optional[dict], due_time: Optional[dict]) -> str:
    if not due_date:
        return ""
    d = due_date
    t = due_time or {}
    return (
        f"{d.get('year','?')}-{d.get('month',1):02d}-{d.get('day',1):02d} "
        f"{t.get('hours',23):02d}:{t.get('minutes',59):02d} UTC"
    )


def _norm_attachments(raw_attachments: list) -> List[AttachmentInfo]:
    return [AttachmentInfo(**a) for a in raw_attachments]


# ══════════════════════════════════════════════════════════════════════════════
# STUDY TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_classes() -> List[CourseInfo]:
    """
    List all Google Classroom courses the student is enrolled in.

    Returns course name, section, and link. Use the course name (or ID)
    with list_modules to explore a specific subject.
    """
    try:
        courses = fetch_courses()
        return [CourseInfo(**c) for c in courses]
    except Exception as exc:
        logger.error("list_classes failed: %s", exc, exc_info=True)
        raise


@mcp.tool()
def list_modules(course_id: str) -> List[ModuleInfo]:
    """
    List all topics / modules inside a specific course.

    Args:
        course_id: The course ID from list_classes (e.g. '123456789').

    Returns modules in creation order. Use the module ID with
    list_materials to see what's inside each module.
    """
    try:
        topics = fetch_topics(course_id)
        if not topics:
            return []
        return [ModuleInfo(**t) for t in topics]
    except Exception as exc:
        logger.error("list_modules failed for %s: %s", course_id, exc, exc_info=True)
        raise


@mcp.tool()
def list_materials(
    course_id: str,
    topic_id: Optional[str] = None,
    include_assignments: bool = True,
    include_announcements: bool = False,
) -> List[MaterialInfo]:
    """
    List all materials inside a course module, or all materials in the course.

    Args:
        course_id: The course ID from list_classes.
        topic_id: Optional module ID from list_modules. If omitted, returns
                  all materials in the course.
        include_assignments: Include assignments (shown as info only — no submission).
        include_announcements: Include teacher announcements.

    Each material shows its title, type, due date (if assignment),
    and list of attachments. Use get_material to read the actual content.

    NOTE: Assignments are shown for information only. ClassPilot does
    not support submitting or turning in work.
    """
    try:
        items: list = []

        mats = fetch_course_work_materials(course_id, topic_id)
        items.extend(mats)

        if include_assignments:
            assignments = fetch_assignments(course_id, topic_id)
            items.extend(assignments)

        if include_announcements:
            anns = fetch_announcements(course_id)
            if topic_id:
                anns = [a for a in anns if a.get("topic_id") == topic_id]
            items.extend(anns)

        result = []
        for item in items:
            due = _fmt_due(item.get("due_date"), item.get("due_time"))
            result.append(MaterialInfo(
                id=item["id"],
                kind=item["kind"],
                title=item["title"],
                description=(item.get("description") or "")[:300],
                state=item.get("state"),
                due_date=due or None,
                attachments=_norm_attachments(item.get("attachments", [])),
                link=item.get("link"),
            ))
        return result

    except Exception as exc:
        logger.error("list_materials failed: %s", exc, exc_info=True)
        raise


@mcp.tool()
def get_material(
    course_id: str,
    material_id: str,
    material_kind: str = "MATERIAL",
    read_attachments: bool = True,
) -> MaterialContent:
    """
    Retrieve a material and read the content of its attached files.

    Args:
        course_id: The course ID from list_classes.
        material_id: The material ID from list_materials.
        material_kind: 'MATERIAL', 'ASSIGNMENT', or 'ANNOUNCEMENT'.
        read_attachments: If True, attempt to read text content from
                          Drive attachments. Set False to get metadata only.

    What ClassPilot can read:
      ✅ Google Docs         — full text content
      ✅ Google Slides       — all slide text
      ✅ Google Sheets       — data as CSV
      ✅ Plain text files    — full content
      ✅ Uploaded .pptx      — text from all slides, tables, and speaker notes
      ⚠️ PDF files           — title and link only (open manually)
      ⚠️ Legacy .ppt         — title and link only (open manually)
      ⚠️ YouTube / links     — URL only

    The LLM can summarize, explain, or answer questions about any
    content that was successfully extracted.
    """
    svc_map = {
        "MATERIAL":     fetch_course_work_materials,
        "ASSIGNMENT":   fetch_assignments,
        "ANNOUNCEMENT": fetch_announcements,
    }
    fetcher = svc_map.get(material_kind.upper(), fetch_course_work_materials)

    try:
        items = fetcher(course_id)
        item = next((i for i in items if i["id"] == material_id), None)
        if not item:
            raise ValueError(
                f"Material {material_id} not found in course {course_id} "
                f"(kind={material_kind}). Check the ID from list_materials."
            )
    except ValueError:
        raise
    except Exception as exc:
        logger.error("get_material fetch failed: %s", exc, exc_info=True)
        raise

    enriched_attachments: list[dict] = []
    for att in item.get("attachments", []):
        entry = dict(att)
        if read_attachments and att.get("type") == "drive" and att.get("id"):
            try:
                file_result = read_drive_file(att["id"])
                entry.update({
                    "readable":  file_result["readable"],
                    "content":   file_result["content"],
                    "mime_type": file_result["mime_type"],
                    "note":      file_result["note"],
                })
            except Exception as exc:
                logger.warning("Could not read attachment %s: %s", att.get("title"), exc)
                entry.update({"readable": False, "content": "", "note": str(exc)})
        enriched_attachments.append(entry)

    return MaterialContent(
        id=item["id"],
        title=item["title"],
        kind=item["kind"],
        description=item.get("description") or "",
        attachments=enriched_attachments,
        link=item.get("link"),
    )


@mcp.tool()
def search_classroom(query: str) -> List[SearchResult]:
    """
    Search across all courses, modules, and materials by title or description.

    Args:
        query: Free text to search for (e.g. 'quantum gates', 'Module 3',
               'superposition', 'Deep Learning').

    Use this when you don't know which course or module contains something.
    Results include the full location context (Course → Module → Material)
    so you can navigate to the right place with list_modules or list_materials.
    """
    try:
        results = search_all(query)
        return [
            SearchResult(
                type=r["type"],
                title=r["title"],
                context=r["context"],
                course_name=r.get("course_name"),
                topic_name=r.get("topic_name"),
                kind=r.get("kind"),
                link=r.get("link"),
            )
            for r in results
        ]
    except Exception as exc:
        logger.error("search_classroom failed for query '%s': %s", query, exc, exc_info=True)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# WATCHER TOOLS (kept from original — still fully working)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_upcoming_deadlines(within_hours: int = 168) -> List[DeadlineInfo]:
    """
    Return upcoming assignment deadlines sorted from soonest to latest.

    Args:
        within_hours: Only include deadlines within this many hours.
                      Default 168 = 7 days. Set to 0 for all future deadlines.
    """
    from .classroom_client import classroom as _classroom
    now = datetime.now(tz=timezone.utc)
    deadlines = []

    try:
        courses = fetch_courses()
    except Exception as exc:
        logger.error("get_upcoming_deadlines: %s", exc)
        return []

    for course in courses:
        cid = course["id"]
        try:
            assignments = _classroom.list_assignments(cid, page_size=100)
        except Exception:
            continue
        for a in assignments:
            due_dt = _parse_due_datetime(a.get("due_date"), a.get("due_time"))
            if not due_dt or due_dt <= now:
                continue
            mins = int((due_dt - now).total_seconds() / 60)
            hrs  = round(mins / 60, 1)
            if within_hours > 0 and hrs > within_hours:
                continue

            d = a.get("due_date") or {}
            t = a.get("due_time") or {}
            deadlines.append(DeadlineInfo(
                course_name=course["name"],
                title=a.get("title", "Untitled"),
                due_date=f"{d.get('year','?')}-{d.get('month',1):02d}-{d.get('day',1):02d}",
                due_time_utc=f"{t.get('hours',23):02d}:{t.get('minutes',0):02d}",
                minutes_remaining=mins,
                hours_remaining=hrs,
                state="ASSIGNMENT",
            ))

    deadlines.sort(key=lambda x: x.minutes_remaining)
    return deadlines


@mcp.tool()
def start_assignment_watcher() -> WatcherResult:
    """
    Start the background assignment watcher.

    When running, ClassPilot polls Classroom every 15 minutes (configurable)
    and sends AI-generated email notifications for new assignments and
    deadline reminders at 1 day / 6 hours / 1 hour / 15 minutes before each due date.
    """
    svc = _get_services()
    if svc.is_watcher_running:
        return WatcherResult(running=True, message="Watcher is already running.")
    svc.start_background_scheduler()
    return WatcherResult(
        running=True,
        message=(
            f"✅ Assignment watcher started. Polling every "
            f"{svc.config.watch_interval_minutes} minutes."
        ),
    )


@mcp.tool()
def stop_assignment_watcher() -> WatcherResult:
    """Stop the background assignment watcher and all pending reminder jobs."""
    svc = _get_services()
    if not svc.is_watcher_running:
        return WatcherResult(running=False, message="Watcher was not running.")
    svc.stop_scheduler()
    return WatcherResult(running=False, message="⏹ Assignment watcher stopped.")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    from .logging_setup import configure_logging
    from .config import get_config
    config = get_config()
    configure_logging(config.log_level)
    logger.info(
        "Starting ClassPilot AI MCP server (streamable-http) on http://%s:%d%s",
        config.mcp_http_host, config.mcp_http_port, config.mcp_http_path,
    )
    mcp.run(
        transport="streamable-http",
        host=config.mcp_http_host,
        port=config.mcp_http_port,
        path=config.mcp_http_path,
    )


if __name__ == "__main__":
    main()

"""
Visual Extractor

Pulls out *important* embedded images (diagrams, charts, graphs, screenshots)
from PPTX, PDF, and DOCX files, and packages them so a vision-capable LLM can
look at them directly — this is NOT OCR. Nothing here reads text out of an
image; it selects which images are worth showing and hands over the raw
image bytes untouched. Any "understanding" of the image happens on the
connected AI's side, the same way a human would look at a slide.

This module is intentionally independent of study_client.py's text
extractors (_extract_pptx_text / _extract_pdf_text / _extract_docx_text):
it does its own lightweight walk of the same file bytes to find picture
shapes. That duplication is deliberate — it means image extraction can be
added, changed, or disabled without touching the existing, working text
extraction path at all.

Filtering approach (why an image is kept or skipped):
  1. Size filter    — an image must cover a meaningful fraction of the
                       page/slide to be considered content rather than a
                       small icon or bullet-point glyph.
  2. Repetition filter — an image whose exact bytes recur across many
                       pages/slides (a running header logo, a watermark, a
                       template background) is decorative, not content, and
                       is dropped everywhere it appears.
  3. Cap            — at most MAX_VISUALS_PER_FILE images are returned per
                       file, prioritising diagram-only pages/slides (where
                       the image *is* the entire content) over pages that
                       already have substantial extracted text.

Returned visuals are plain dicts (no PIL/FastMCP types leak out of this
module):
    {
        "mime_type": "image/png",
        "data":      b"...",          raw image bytes
        "location":  "Slide 5",       human-readable, for captioning
        "caption":   "Slide 5 — diagram (no other text on this slide)",
        "diagram_only": True,
        "area_ratio": 0.62,           image area / page area, 0-1
    }
"""

import hashlib
import io
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Tuning constants — kept together and named so the heuristic is easy to
# read and adjust without hunting through the extraction logic below.
MIN_AREA_RATIO = 0.05          # image must cover >=5% of the page/slide area
MIN_DIAGRAM_ONLY_AREA_RATIO = 0.02  # a lower bar when it's the ONLY content on the page
MAX_VISUALS_PER_FILE = 8       # hard cap so one file can't flood the response
REPEATED_IMAGE_MIN_COUNT = 3   # same image bytes on >=3 pages/slides -> treat as logo/watermark


def _image_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _drop_repeated_images(candidates: list[dict]) -> list[dict]:
    """
    Remove images whose exact bytes repeat often enough to be a running
    logo, watermark, or template background rather than page content.
    """
    counts: dict[str, int] = {}
    for c in candidates:
        h = c["_hash"]
        counts[h] = counts.get(h, 0) + 1

    kept = [c for c in candidates if counts[c["_hash"]] < REPEATED_IMAGE_MIN_COUNT]
    dropped = len(candidates) - len(kept)
    if dropped:
        logger.debug("visual_extractor: dropped %d repeated (logo/watermark) images", dropped)
    return kept


def _rank_and_cap(candidates: list[dict], max_visuals: int) -> list[dict]:
    """
    Diagram-only pages/slides first (the image IS the content — otherwise
    that page would be invisible to the LLM), then largest-area first.
    Ties broken by original page/slide order.
    """
    ranked = sorted(
        candidates,
        key=lambda c: (not c["diagram_only"], -c["area_ratio"], c["_order"]),
    )
    return ranked[:max_visuals]


def _finalise(candidates: list[dict], max_visuals: int) -> list[dict]:
    kept = _drop_repeated_images(candidates)
    kept = _rank_and_cap(kept, max_visuals)
    kept.sort(key=lambda c: c["_order"])  # restore natural page/slide order for output
    for c in kept:
        c.pop("_hash", None)
        c.pop("_order", None)
    return kept


# ── PPTX ─────────────────────────────────────────────────────────────────────

def extract_pptx_visuals(buf: io.BytesIO, max_visuals: int = MAX_VISUALS_PER_FILE) -> list[dict]:
    """
    Find important pictures embedded in a .pptx deck's slides.

    A slide with a picture and no other extracted text (e.g. a diagram-only
    slide) is always eligible even if the image is comparatively small,
    since it's the only content that slide has to offer.
    """
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    buf.seek(0)
    prs = Presentation(buf)
    slide_area = max(prs.slide_width * prs.slide_height, 1)

    candidates: list[dict] = []
    order = 0

    for idx, slide in enumerate(prs.slides, start=1):
        slide_has_text = False
        picture_shapes = []

        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                slide_has_text = True
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                picture_shapes.append(shape)

        for shape in picture_shapes:
            try:
                image = shape.image
            except Exception as exc:
                logger.debug("Skipping unreadable picture on slide %d: %s", idx, exc)
                continue

            area_ratio = (shape.width * shape.height) / slide_area
            diagram_only = not slide_has_text
            threshold = MIN_DIAGRAM_ONLY_AREA_RATIO if diagram_only else MIN_AREA_RATIO
            if area_ratio < threshold:
                continue  # too small to be meaningful content (icon/bullet glyph)

            order += 1
            candidates.append({
                "mime_type":    image.content_type or "image/png",
                "data":         image.blob,
                "location":     f"Slide {idx}",
                "caption": (
                    f"Slide {idx} — diagram (this slide has no other extractable text)"
                    if diagram_only else f"Slide {idx} — embedded image/chart"
                ),
                "diagram_only": diagram_only,
                "area_ratio":   round(area_ratio, 3),
                "_hash":        _image_hash(image.blob),
                "_order":       order,
            })

    return _finalise(candidates, max_visuals)


# ── PDF ──────────────────────────────────────────────────────────────────────

def extract_pdf_visuals(buf: io.BytesIO, max_visuals: int = MAX_VISUALS_PER_FILE) -> list[dict]:
    """
    Find important images embedded in a PDF's pages.

    Uses pypdf's per-page `.images`, which decodes each embedded XObject
    image to bytes plus a PIL `.image` for pixel dimensions. A page whose
    text layer is empty but that has an image is treated as diagram-only
    (e.g. a slide-per-page export, or a scanned figure).
    """
    from pypdf import PdfReader

    buf.seek(0)
    reader = PdfReader(buf)

    candidates: list[dict] = []
    order = 0

    for idx, page in enumerate(reader.pages, start=1):
        page_text = (page.extract_text() or "").strip()
        page_has_text = bool(page_text)

        try:
            box = page.mediabox
            page_area = max(float(box.width) * float(box.height), 1)
        except Exception:
            page_area = None

        try:
            images = page.images
        except Exception as exc:
            logger.debug("Could not read images on PDF page %d: %s", idx, exc)
            continue

        for img in images:
            try:
                data = img.data
                pil_img = img.image  # Pillow Image; python-pptx already requires Pillow
                width, height = pil_img.size if pil_img else (None, None)
            except Exception as exc:
                logger.debug("Skipping unreadable PDF image on page %d: %s", idx, exc)
                continue

            if page_area and width and height:
                area_ratio = (width * height) / page_area
            else:
                area_ratio = None  # can't compare to page size — fall back below

            diagram_only = not page_has_text
            threshold = MIN_DIAGRAM_ONLY_AREA_RATIO if diagram_only else MIN_AREA_RATIO

            if area_ratio is not None and area_ratio < threshold:
                continue
            # If we couldn't compute an area ratio at all, only keep the
            # image when it's the page's only content (diagram-only) —
            # otherwise we'd have no basis to filter out small icons.
            if area_ratio is None and not diagram_only:
                continue

            mime = f"image/{(pil_img.format or 'png').lower()}" if pil_img else "image/png"
            order += 1
            candidates.append({
                "mime_type":    mime,
                "data":         data,
                "location":     f"Page {idx}",
                "caption": (
                    f"Page {idx} — diagram/figure (this page has no other extractable text)"
                    if diagram_only else f"Page {idx} — embedded image/chart"
                ),
                "diagram_only": diagram_only,
                "area_ratio":   round(area_ratio, 3) if area_ratio is not None else None,
                "_hash":        _image_hash(data),
                "_order":       order,
            })

    return _finalise(candidates, max_visuals)


# ── DOCX ─────────────────────────────────────────────────────────────────────

def extract_docx_visuals(buf: io.BytesIO, max_visuals: int = MAX_VISUALS_PER_FILE) -> list[dict]:
    """
    Find important images embedded in a .docx document.

    Word documents don't have a page-by-page text/image association in the
    object model the way slides or PDF pages do, so "diagram_only" isn't
    meaningful here — every kept image is filtered purely by size relative
    to the page width (a reasonable proxy for "this is a real figure, not
    an inline icon").
    """
    from docx import Document as DocxDocument

    buf.seek(0)
    doc = DocxDocument(buf)

    try:
        page_width = doc.sections[0].page_width or 0
        page_height = doc.sections[0].page_height or 0
        page_area = max(page_width * page_height, 1)
    except Exception:
        page_area = None

    candidates: list[dict] = []
    order = 0

    for idx, shape in enumerate(doc.inline_shapes, start=1):
        try:
            inline = shape._inline
            blip = inline.graphic.graphicData.pic.blipFill.blip
            rId = blip.embed
            part = doc.part.related_parts[rId]
            data = part.blob
            mime = part.content_type or "image/png"
        except Exception as exc:
            logger.debug("Skipping unreadable DOCX inline image #%d: %s", idx, exc)
            continue

        if page_area and shape.width and shape.height:
            area_ratio = (shape.width * shape.height) / page_area
        else:
            area_ratio = None

        if area_ratio is not None and area_ratio < MIN_AREA_RATIO:
            continue
        if area_ratio is None:
            continue  # no size signal at all — safer to skip than spam every inline image

        order += 1
        candidates.append({
            "mime_type":    mime,
            "data":         data,
            "location":     f"Image {idx}",
            "caption":      f"Embedded figure {idx}",
            "diagram_only": False,
            "area_ratio":   round(area_ratio, 3),
            "_hash":        _image_hash(data),
            "_order":       order,
        })

    return _finalise(candidates, max_visuals)


# ── Direct image attachments (PNG/JPG/JPEG) ────────────────────────────────────

def prepare_standalone_image(data: bytes, mime_type: str, name: str) -> list[dict]:
    """
    Package a directly-attached image file (not embedded in a document) as
    a visual. No size/repetition filtering applies — unlike an image
    embedded among many shapes in a slide/page/document, a standalone image
    attachment *is* the material, so it's always considered significant.
    """
    return [{
        "mime_type":    mime_type or "image/png",
        "data":         data,
        "location":     name or "Image",
        "caption":      name or "Attached image",
        "diagram_only": True,
        "area_ratio":   1.0,
    }]

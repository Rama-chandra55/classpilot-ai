# ClassPilot AI — Document & Visual Extraction Capabilities

ClassPilot AI is built to provide LLMs with authentic, rich academic context. Rather than merely returning download links or metadata, ClassPilot parses and extracts real content from course materials in real time.

---

## 1. Supported Document & File Formats

When an AI calls `get_material`, ClassPilot inspects each Google Drive attachment by MIME type and extracts its contents using specialized, in-memory parsers.

| Format | Extension / MIME Type | Text Extraction | Visual Extraction | Extraction Method |
|---|---|:---:|:---:|---|
| **Google Docs** | `application/vnd.google-apps.document` | ✅ Full text | — | Google Drive `export_media` to `text/plain` |
| **Google Slides** | `application/vnd.google-apps.presentation` | ✅ All slides | — | Google Drive `export_media` to `text/plain` |
| **Google Sheets** | `application/vnd.google-apps.spreadsheet` | ✅ Full table | — | Google Drive `export_media` to `text/csv` |
| **Plain Text** | `.txt`, `.md`, `.html`, `.csv`, `.json`, `.xml` | ✅ Full text | — | Direct Drive download (`utf-8` decoding) |
| **PowerPoint** | `.pptx` (Office Open XML) | ✅ Slide text, tables, notes | ✅ Charts & diagrams | Parsed with `python-pptx` |
| **Adobe PDF** | `.pdf` | ✅ Page-by-page text | ✅ Charts & diagrams | Parsed with `pypdf` |
| **Microsoft Word** | `.docx` (Office Open XML) | ✅ Paragraphs & tables | ✅ Embedded figures | Parsed with `python-docx` |
| **Standalone Images** | `.png`, `.jpg`, `.jpeg` | — | ✅ Full image | Direct Drive stream (handed over as visual) |
| **Legacy PowerPoint** | `.ppt` (pre-2007 binary) | ⚠️ Link & title only | — | Binary format unsupported; link provided |
| **Legacy Word** | `.doc` (pre-2007 binary) | ⚠️ Link & title only | — | Binary format unsupported; link provided |
| **External Links** | URLs, Google Forms | ⚠️ Link & title only | — | Metadata provided; external sites not crawled |
| **YouTube Videos** | `youtube.com`, `youtu.be` | ⚠️ Link & title only | — | Video URL provided; transcripts not fetched |

> **Graceful Degradation**: If an attachment cannot be parsed (e.g. an encrypted file or a legacy binary document), `get_material` still returns its title, direct web link, and a descriptive `note` explaining the reason.

---

## 2. Visual Content & Diagram Extraction

### The Problem
Academic lecture slides and research papers frequently contain pages that are **pure diagrams** (system architectures, mathematical graphs, biochemical pathways, circuit diagrams) with little or no extractable text. A pure-text scraper leaves the AI assistant completely blind to the diagram.

### How ClassPilot Solves This (Not OCR)
ClassPilot AI does not run server-side OCR on images. Instead, it extracts the original, high-resolution image bytes directly from `.pptx`, `.pdf`, and `.docx` archives and hands them to the AI client as native MCP **Image Content blocks** alongside the JSON text payload.

Modern multimodal AI models (Claude 3.5 Sonnet, GPT-4o, Gemini 1.5 Pro) inspect and reason about these diagrams directly using native computer vision.

### Visual Significance & Filtering Pipeline

To avoid overwhelming LLM context windows with decorative clutter, ClassPilot applies an intelligent three-stage filtering pipeline ([`classpilot/visual_extractor.py`](../classpilot/visual_extractor.py)):

```
Input File (PPTX / PDF / DOCX)
               │
               ▼
┌──────────────────────────────┐
│     1. Candidate Extraction  │  Walks shapes / PDF XObjects / DOCX XML
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│     2. Significance Filter   │  • Standard: area ratio >= 5%
│                              │  • Diagram-only page: area ratio >= 2%
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│     3. Repetition Filter     │  • SHA-256 byte deduplication
│                              │  • Drops logos/watermarks appearing across >= 3 pages
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│     4. Ranking & Capping     │  • Diagram-only pages prioritized first
│                              │  • Max 8 images per file
│                              │  • Max 8 images per get_material response
└──────────────┬───────────────┘
               │
               ▼
Output: FastMCP Image Blocks + JSON Metadata
```

1. **Size Ratio Heuristic**:
   - `MIN_AREA_RATIO = 0.05`: Standard images must cover at least 5% of the slide/page area to be treated as content rather than an icon or bullet symbol.
   - `MIN_DIAGRAM_ONLY_AREA_RATIO = 0.02`: On slides or pages with zero extractable text layer, the threshold is relaxed to 2%, ensuring standalone diagrams are never omitted.
2. **Repetition & Watermark Deduplication**:
   - `REPEATED_IMAGE_MIN_COUNT = 3`: Each candidate image's raw bytes are hashed via SHA-256. If the identical image recurs across 3 or more slides or pages, it is identified as a repeating slide template, running header logo, or watermark, and dropped globally.
3. **Ranking and Hard Capping**:
   - Diagram-only pages (where the image is the sole carrier of meaning) are prioritized first.
   - Secondary ranking sorts by descending surface area ratio.
   - Hard cap of **8 visuals per file** (`MAX_VISUALS_PER_FILE = 8`) and **8 visuals per response** (`_MAX_VISUALS_PER_RESPONSE = 8`) guarantees predictable payload sizes.

---

## 3. Explicit Limitations & Boundaries

To preserve safety and technical accuracy, the following boundaries are enforced:

- **Read-Only Scope**: ClassPilot AI does not support submitting assignments, modifying grades, or altering course material.
- **Server-Side OCR**: Scanned PDFs with no embedded text layer will not have their text extracted server-side.
- **DOCX Slide Boundaries**: Unlike PPTX and PDF, DOCX files do not have a discrete "page" concept in their underlying WordprocessingML schema; images in DOCX are evaluated by absolute dimensions rather than slide-level text-absence heuristics.
- **Embedded Slides/Docs Images**: Google Docs and Google Slides read through the Drive export endpoint return formatted plaintext/CSV; embedded images inside native Google Slides files are not currently unpacked as separate image blocks.
- **External Network Access**: ClassPilot AI does not crawl external websites linked from course announcements or scrape YouTube videos.

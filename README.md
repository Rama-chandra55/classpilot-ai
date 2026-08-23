# ClassPilot AI 🤖

> Your Google Classroom Study Assistant — exposed as an MCP server so any
> MCP-compatible client (Claude Desktop, Cursor, ChatGPT, etc.) can navigate
> your courses and read your course materials on your behalf.
> **100% free to run** using Google Gemini or Groq as the LLM provider.

---

## What ClassPilot AI does today

ClassPilot AI is an [MCP](https://modelcontextprotocol.io) server built on
top of Google Classroom + Google Drive. It exposes **8 tools** that an LLM
client can call directly — 5 for browsing and reading course content, plus
3 carried over from the original assignment watcher.

### Study Assistant tools (primary focus)

| Tool | What it does |
|---|---|
| `list_classes` | List all active Classroom courses you're enrolled in |
| `list_modules` | List the topics/modules inside a course |
| `list_materials` | List materials, assignments, and (optionally) announcements in a module or course |
| `get_material` | Fetch a material and **read the text content of its attachments** |
| `search_classroom` | Search course names, module names, and material titles/descriptions |

Typical navigation flow: `list_classes` → `list_modules` → `list_materials`
→ `get_material`. Use `search_classroom` when you don't know exactly where
something lives.

### Watcher tools (kept from the original project)

| Tool | What it does |
|---|---|
| `get_upcoming_deadlines` | List upcoming assignment due dates, soonest first |
| `start_assignment_watcher` | Start background polling + email notifications for new assignments and deadlines |
| `stop_assignment_watcher` | Stop the background watcher |

**ClassPilot does not submit or turn in assignments.** Assignments are
surfaced as read-only information (title, description, due date) via
`list_materials` / `get_material` only.

---

## Supported file formats

`get_material` attempts to extract real text from every Drive attachment on
a material so the LLM can summarize, explain, or answer questions about it
directly — instead of just handing back a link.

| Format | Text | Visuals | How |
|---|---|---|---|
| Google Docs | ✅ Full text | — | Docs API export |
| Google Slides | ✅ Full text (all slides) | — | Drive export to `text/plain` |
| Google Sheets | ✅ Full data | — | Drive export to CSV |
| Plain text (`.txt`, `.md`, `.html`, `.csv`, `.json`, `.xml`) | ✅ Full content | — | Direct Drive download |
| PowerPoint (`.pptx`) | ✅ Full text — slide text, tables, and speaker notes | ✅ Diagrams/charts | `python-pptx` |
| PDF (`.pdf`) | ✅ Full text, per page | ✅ Diagrams/figures | `pypdf` |
| Word (`.docx`) | ✅ Full text — paragraphs and tables | ✅ Embedded figures | `python-docx` |
| Image (`.png`, `.jpg`/`.jpeg`) | — (not OCR'd) | ✅ Whole image | Direct Drive download |
| Legacy PowerPoint (`.ppt`) | ⚠️ Not supported — title + link only | — | — |
| Legacy Word (`.doc`) | ⚠️ Not supported — title + link only | — | — |
| YouTube videos | ⚠️ Not supported — title + link only | — | — |
| External links | ⚠️ Not supported — title + link only | — | — |

If a file can't be read, `get_material` still returns its title and a
direct link so you (or the LLM) can open it manually, along with a `note`
explaining why (e.g. scanned/image-only PDF with no extractable text).

**Not yet implemented — planned:** YouTube video transcripts, fetching
external link content. Legacy `.ppt`/`.doc` binary formats are not
currently planned since `.pptx`/`.docx` cover the vast majority of
uploaded coursework.

---

## Visual content — diagrams, charts, and images

Slide decks and PDFs often have pages that are *pure diagram* — a
system-architecture drawing, a graph, a labeled figure — with no text
extractable from them at all. `get_material` now pulls out these images
and hands them to the connected AI as real, viewable images (separate MCP
image content blocks alongside the JSON), so it can actually look at them
instead of only seeing "there was an image here."

**This is not OCR.** No pixels are read into text; ClassPilot decides
*which* images are worth showing and passes the raw image bytes through
unchanged. Any understanding of a diagram's content happens on the
connected AI's (vision-capable) side, same as if a person looked at the
slide.

**What counts as "important" — the filtering rules ([`visual_extractor.py`](classpilot/visual_extractor.py)):**
- **Size** — an image must cover a meaningful share of the slide/page to
  be treated as content. Diagram-only slides/pages (no other text at all)
  get a lower bar, since the image *is* the entire content there.
- **Repetition** — an image whose exact bytes repeat across 3+
  slides/pages (a running header logo, a watermark, a template background)
  is treated as decorative and dropped everywhere it appears.
- **Cap** — at most 8 images per file (diagram-only slides/pages
  prioritized first, then largest), and 8 total per `get_material` call
  across all of a material's attachments — so one huge deck can't flood
  the response.

| Source | Visual detection |
|---|---|
| PPTX | Per-slide: is there *any* other text on this slide? If not, its image(s) are diagram-only and get priority. |
| PDF | Same idea, per page, using each page's extracted text layer. |
| DOCX | No natural page/text boundary in the object model, so every embedded inline image is filtered by size relative to the page only (not diagram-only vs. not). |
| Direct image attachment (`.png`/`.jpg`) | The whole file is handed over — it *is* the material, so no filtering applies. |

**Known limitations:**
- DOCX can't distinguish "diagram-only" the way PPTX/PDF can (see table above) — every embedded image is judged purely on size.
- A diagram that's small relative to its slide/page (e.g. a small inline formula image) may be filtered out along with genuine icons — the size heuristic can't fully distinguish "small but important" from "small and decorative."
- Images inside Google Slides/Docs (as opposed to uploaded `.pptx`/`.docx`) aren't covered yet — those materials are read via Drive's text export, which doesn't carry embedded image bytes.
- Legacy `.ppt`/`.doc` and non-PNG/JPEG image types (`.gif`, `.webp`) are unaffected by this feature — still title+link only.

---

## Pagination

Every Classroom list read (`courses`, `topics`, `courseWorkMaterials`,
`courseWork`, `announcements`) follows the API's `nextPageToken` to
completion. Courses with more modules, materials, or assignments than fit
in a single API response page are not silently truncated.

---

## Project status

- **Study Assistant (5 tools):** working — this is the actively developed part of the project.
- **PDF / DOCX / PPTX text extraction:** working, tested.
- **Visual content (diagrams/charts/images):** working, tested — see "Visual content" above for what's covered and current limitations.
- **Pagination:** working across all Classroom list reads.
- **Assignment Watcher (3 tools):** working, carried over unmodified in behavior from the original project.
- **Assignment submission:** intentionally removed from the MCP surface. The underlying vendor Classroom API code and `classpilot/submission_flow.py` still exist in the repo (and are still covered by tests) but are **not** registered as MCP tools in `classpilot/server.py`, and there are no plans to re-expose them.

---

## Architecture

```
classpilot/
  server.py            MCP server — registers all 8 tools (stdio transport)
  http_server.py        Same server over Streamable HTTP (for remote/ChatGPT clients)
  study_client.py        Raw Classroom/Drive calls + file-content extraction for the 5 study tools
  visual_extractor.py     Diagram/chart/image extraction + significance filtering for
                          PPTX/PDF/DOCX and direct image attachments (not OCR — selects and
                          hands over raw image bytes for the connected AI to look at)
  classroom_client.py     Thin wrapper that re-exports the vendor package and extends its
                          OAuth scopes (courseworkmaterials, announcements, topics) without
                          modifying vendor code
  submission_flow.py      Two-step submission flow (attach → confirm → turn in) — kept for the
                          watcher's internal use in tests; NOT exposed as an MCP tool
  watcher.py, deadline_scheduler.py, scheduler.py, state_store.py
                          Background polling, deduplication, and deadline-reminder scheduling
  notification_service.py, notifier/
                          LLM-generated notification text + delivery (email today)
  llm/                    Pluggable LLM providers (Gemini, Groq, Anthropic)
  main.py                 Standalone watcher entrypoint (no MCP, just polling + notifications)

vendor_classroom_suite_mcp/
                          Vendored, unmodified third-party Classroom/Drive/Docs client.
                          classroom_client.py wraps this rather than forking it.
```

---

## Supported LLM Providers

| Provider | Cost | Sign up |
|---|---|---|
| **Google Gemini** ✅ Recommended | **Free** (15 req/min, 1M tokens/day) | [aistudio.google.com](https://aistudio.google.com/app/apikey) — same Google account you use for Classroom |
| **Groq** ✅ Also free | **Free** (generous daily limits, very fast) | [console.groq.com](https://console.groq.com) |
| Anthropic Claude | Paid | Only needed if you specifically want Claude for notification text |

The LLM is only used to generate the funny/friendly text in watcher email
notifications — it is not required for the Study Assistant tools
(`list_classes`, `get_material`, etc.), which return raw Classroom/Drive
data directly.

---

## Setup

1. **Google Cloud project** — enable the Google Classroom API and Google
   Drive API, create OAuth 2.0 credentials (Desktop app type), download as
   `credentials.json` into the project root.
2. **Python environment**
   ```bash
   python -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\activate
   pip install -e .
   ```
3. **Configure**
   ```bash
   cp .env.example .env
   # fill in LLM_API_KEY (Gemini or Groq) and, if you want email
   # notifications from the watcher, the SMTP_* variables.
   ```
4. **First run — authenticate**
   ```bash
   classpilot-ai-mcp
   ```
   A browser window opens for Google login on first run; a token is saved
   to `GOOGLE_TOKEN_PATH` (default `token.json`) for future runs.

   > If you've used an older version of this project before the study
   > scopes (`courseworkmaterials`, `announcements`, `topics.readonly`)
   > were added, delete `token.json` once and re-authenticate so Google
   > issues a token that includes them.

### Running modes

| Command | Transport | Use case |
|---|---|---|
| `classpilot-ai-mcp` | stdio | Claude Desktop, Cursor, and other local MCP clients |
| `classpilot-ai-http` | Streamable HTTP (`/mcp`, default `127.0.0.1:8000`) | Remote clients, e.g. a ChatGPT custom connector |
| `classpilot-ai` | none (no MCP) | Standalone watcher — polling + email notifications only, no chat client involved |

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` (free), `groq` (free), `anthropic` (paid) — used for watcher notification text only |
| `LLM_API_KEY` | — | Your API key for the chosen provider |
| `LLM_MODEL` | `gemini-1.5-flash` | Model name |
| `WATCH_INTERVAL_MINUTES` | `15` | Classroom polling interval for the watcher |
| `STATE_DB_PATH` | `classpilot_state.db` | SQLite dedup database for the watcher |
| `NOTIFIER_BACKEND` | `email` | `email` (more coming) |
| `SMTP_HOST` | — | SMTP server |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | — | Sender email |
| `SMTP_PASSWORD` | — | SMTP password or App Password |
| `NOTIFY_TO_EMAIL` | — | Where the watcher sends notifications |
| `MCP_HTTP_HOST` | `127.0.0.1` | Bind address for `classpilot-ai-http` |
| `MCP_HTTP_PORT` | `8000` | Bind port for `classpilot-ai-http` |
| `MCP_HTTP_PATH` | `/mcp` | URL path for the MCP endpoint |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `GOOGLE_CREDENTIALS_PATH` | `credentials.json` | Google OAuth credentials |
| `GOOGLE_TOKEN_PATH` | `token.json` | Saved auth token |

---

## Testing

```bash
pip install pytest
python -m pytest tests/ -q
```

All external dependencies (Google APIs, FastMCP, APScheduler, the LLM
SDKs) are stubbed at the `sys.modules` level in each test file, so the
suite needs no credentials and makes no network calls. Image-extraction
tests use the real `python-pptx`/`pypdf`/`python-docx`/`Pillow` libraries
to build tiny in-memory fixture files — no external files or network
access needed there either. A couple of PDF-fixture tests additionally
use `reportlab` (not a runtime dependency) to generate test PDFs; they
skip automatically if it isn't installed.

---

## Troubleshooting

**`credentials.json not found`**
Download from Google Cloud → Credentials and place in the project folder.

**`LLM_API_KEY is not set`**
Only required for watcher notifications. Fill in `LLM_API_KEY` in `.env`,
or ignore it if you're only using the Study Assistant tools.

**No email received from the watcher**
Check `SMTP_USER`, `SMTP_PASSWORD`, `NOTIFY_TO_EMAIL`. Gmail users need an
App Password. Check your spam folder. Set `LOG_LEVEL=DEBUG` for details.

**"This app isn't verified" during Google login**
Expected. Click **Advanced → Go to ClassPilot AI (unsafe) → Allow**. It's
your own app.

**Google login required again, or `list_materials`/`get_material` returns
403 / missing scope errors**
Delete `token.json` and restart — a browser will open for a fresh login
that includes the current scope set (Classroom, Drive, Docs,
courseworkmaterials, announcements, topics).

**`get_material` says a file "cannot be read" for a `.pdf` or `.docx`**
The file may be scanned/image-only with no extractable text layer, or the
file may not actually be a modern `.pdf`/`.docx` (e.g. it's a legacy
`.doc`/`.ppt`, or an image). Open the returned `link` to view it manually.

**A diagram-only slide/page doesn't show up as an image**
Check its size relative to the slide/page — very small diagrams can fall
below the significance threshold along with genuine icons (see "Visual
content" above). If a file has many candidate images, only the top 8 (by
diagram-only status, then size) are included per response.

---

## License

ClassPilot AI is your own application.
The bundled `vendor_classroom_suite_mcp` is MIT-licensed — see
`vendor_classroom_suite_mcp/LICENSE`.

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
- **Multi-user persistence foundation (Phase 1 of the multi-user roadmap):** working, tested — Postgres-backed `users` + encrypted Google-credential storage exist and are covered by real integration tests, but are **not yet wired into the live auth flow**. The app still runs single-user via `token.json` today; see "Multi-user roadmap" below.

---

## Multi-user roadmap

ClassPilot AI is being extended from a single-user tool (one `token.json`)
into a multi-user MCP server every student can connect their own Google
account to. This is being built in phases so each one can be reviewed and
tested independently rather than landing as one large, risky change.

**Phase 1 (done) — Multi-user persistence foundation**
- `classpilot/db.py` — Postgres connection pool + schema management
- `classpilot/crypto.py` — Fernet encryption for stored Google refresh tokens
- `classpilot/user_store.py` — `users` + `google_oauth_credentials` CRUD
- `classpilot/state_store.py` — additive `user_id` column/param (schema readiness; still defaults to a single tenant today)
- Google OAuth scope audit — see "Google OAuth scopes" below

**Phase 2 (done) — Google web OAuth flow for multiple users**
- `classpilot/google_oauth.py` — authorization URL, code exchange, ID token
  verification (identity keyed on Google's `sub`, not email), and
  `get_authorized_credentials(user_id)` for building a live, refreshed
  `Credentials` object on demand — refreshing only when the cached access
  token is missing/expired, not on every call, and never overwriting a
  stored refresh token with `None` if a later re-authorization doesn't
  return a new one
- `classpilot/oauth_state.py` — Postgres-backed, single-use, expiring CSRF
  state tokens, paired with the PKCE `code_verifier` each login attempt
  generates (see the module's docstring — the verifier only ever lives on
  a single, short-lived `Flow` object in memory, so it has to be persisted
  somewhere to survive the trip from `/login` to `/callback`; that
  "somewhere" is this table, not a process-global variable)
- `classpilot/oauth_web.py` — standalone Starlette app (`classpilot-ai-oauth`)
  exposing `/auth/google/login` and `/auth/google/callback` — **not**
  wired into `server.py`/`http_server.py`'s MCP transport at all, so
  nothing here is reachable from an MCP client and no Google token value
  is ever included in an HTTP response

Like Phase 1, this is additive: `token.json` + the single global auth
singleton in `vendor_classroom_suite_mcp` are still what actually
authenticates every live MCP request today. The web flow above lets you
connect additional students' Google accounts into the Postgres store, but
nothing in `server.py`/`study_client.py` reads from that store yet — see
"Manually testing two Google accounts" below for how to verify the flow
itself, independent of the (still single-user) running app.

**Not yet started:**
- **Phase 3** — thread real per-user identity through `study_client.py`/`server.py`'s call path and the watcher's polling loop (the biggest, highest-risk step), and add MCP-facing OAuth 2.1 so Claude/Cursor/ChatGPT can each connect their own student
- **Phase 4** — production deployment (real TLS host, managed Postgres, secrets manager, Google App Verification)

---

## Google web OAuth setup (Phase 2)

This is separate from, and doesn't affect, the app's normal single-user
`token.json` flow — it's for connecting *additional* students' Google
accounts into the Postgres store built in Phase 1, and for testing the web
OAuth flow itself.

**1. Google Cloud Console — create a second OAuth client**
   - Your existing `credentials.json` is a **Desktop app** type client used
     by the `token.json` fallback flow — leave it alone.
   - Create a **new, separate** OAuth client: Credentials → Create
     Credentials → OAuth client ID → **Web application**.
   - Add an Authorized redirect URI: `http://localhost:8090/auth/google/callback`
     (or whatever `GOOGLE_OAUTH_REDIRECT_URI` you configure).
   - Download its JSON and save it as `google_web_credentials.json` (or point
     `GOOGLE_WEB_CREDENTIALS_PATH` at wherever you put it).
   - Under your OAuth consent screen's **Test users**, add every Google
     account you want to test with (while the app is in Testing publishing
     status, Google restricts sign-in to explicitly listed test users).

**2. Add to `.env`:**
   ```
   GOOGLE_WEB_CREDENTIALS_PATH=google_web_credentials.json
   GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8090/auth/google/callback
   ```
   (`DATABASE_URL` and `TOKEN_ENCRYPTION_KEY` from Phase 1's setup are reused as-is.)

**3. Run the standalone OAuth web app:**
   ```bash
   classpilot-ai-oauth
   ```

### Manually testing two Google accounts

1. Visit `http://localhost:8090/auth/google/login` in a browser → sign in
   as **Account A** → grant consent → you'll land on a plain "Connected!"
   page showing Account A's email.
2. Open an incognito/private window (or sign out of Account A first) →
   visit the same login URL → sign in as **Account B** → grant consent.
3. Confirm two distinct users were created:
   ```bash
   psql "$DATABASE_URL" -c "SELECT google_sub, email FROM users;"
   psql "$DATABASE_URL" -c "SELECT user_id FROM google_oauth_credentials;"
   ```
   You should see two rows in each — different `google_sub` values, even
   if (hypothetically) the accounts shared an email alias, since identity
   is keyed on the stable Google `sub`, never on email.
4. Confirm each user's credentials actually authenticate as *that* Google
   account (proves the refresh round-trip and per-user isolation, not
   just that rows exist):
   ```python
   from classpilot.user_store import UserStore
   from classpilot.google_oauth import get_authorized_credentials
   from googleapiclient.discovery import build

   store = UserStore()
   for user in [store.get_user_by_google_sub("<sub-A>"), store.get_user_by_google_sub("<sub-B>")]:
       creds = get_authorized_credentials(user.id, store)
       svc = build("classroom", "v1", credentials=creds)
       courses = svc.courses().list(pageSize=5).execute()
       print(user.email, "->", [c["name"] for c in courses.get("courses", [])])
   ```
5. **CSRF check:** try re-visiting a callback URL with a `state` value
   you've already used (e.g. reload the browser tab after step 1 completes)
   — it must show "Sign-in link expired or invalid", not process again.

---

## Google OAuth scopes

The exact set of scopes ClassPilot requests, and why — audited against
what the codebase actually calls (not assumed):

| Scope | Used for |
|---|---|
| `classroom.courses.readonly` | `list_classes` |
| `classroom.coursework.me` | `get_upcoming_deadlines`, assignment listing |
| `classroom.courseworkmaterials` | `list_materials` |
| `classroom.announcements` | `list_materials` (announcements) |
| `classroom.topics.readonly` | `list_modules` |
| `drive.readonly` | `get_material` reading PPTX/PDF/DOCX/image attachments and exporting Google Docs/Slides/Sheets |

**Deliberately not requested** (removed from the vendored client's
broader default, since nothing in the exposed tool surface uses them):
- `classroom.coursework.students` — only relevant to the vendored, unexposed `list_submissions()`, which needs teacher-level visibility into every student's submissions; not applicable to a student-facing app reading its own coursework.
- `classroom.rosters.readonly` — no roster endpoint is called anywhere in this codebase.
- `documents` (Google Docs API) — Google Docs content is read via Drive's `export_media`, never via the Docs API itself.
- Full `drive` (read/write) — every active Drive call is read-only; `drive.readonly` covers `.get()`, `.get_media()`, and `.export_media()`.

If a future phase re-exposes the vendored submission-upload flow
(`classpilot/submission_flow.py`), it will need its own broader scope back
— deliberately, not by leaving it granted unused today.

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
  classroom_client.py     Thin wrapper that re-exports the vendor package, curates its
                          OAuth scope list (see "Google OAuth scopes" above), without
                          modifying vendor code
  db.py                   Postgres connection pool + schema management (Phase 1)
  crypto.py                Fernet encryption for stored Google refresh tokens (Phase 1)
  user_store.py            users + google_oauth_credentials CRUD (Phase 1; not yet wired
                          into the live auth flow — see "Multi-user roadmap" above)
  google_oauth.py           Google web OAuth: authorization URL, code exchange, ID token
                          verification, refresh-only-when-needed credential caching (Phase 2)
  oauth_state.py            Postgres-backed, single-use, expiring CSRF state tokens (Phase 2)
  oauth_web.py              Standalone Starlette app (classpilot-ai-oauth) — login/callback
                          routes; NOT part of the MCP transport, not reachable from any
                          MCP client (Phase 2)
  submission_flow.py      Two-step submission flow (attach → confirm → turn in) — kept for the
                          watcher's internal use in tests; NOT exposed as an MCP tool
  watcher.py, deadline_scheduler.py, scheduler.py, state_store.py
                          Background polling, deduplication, and deadline-reminder scheduling
                          (state_store.py: SQLite, now with a schema-ready but not-yet-wired
                          user_id column/param — see "Multi-user roadmap" above)
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
4. **Database setup (Phase 1 — optional unless you're working on the
   multi-user persistence code itself)**

   Not required to run the app today — `token.json` still drives the live
   single-user auth flow. This sets up the Postgres-backed `users`/
   credential storage the multi-user roadmap will wire in later, and is
   needed to run `tests/test_user_store.py`'s integration tests.

   ```bash
   # Install Postgres locally (Ubuntu/Debian shown; use your platform's
   # package manager / a managed instance otherwise)
   sudo apt-get install -y postgresql postgresql-contrib
   sudo service postgresql start

   # Create a role and two databases — `classpilot` for normal dev use,
   # `classpilot_test` kept separate so running tests never touches dev data
   sudo -u postgres psql -c "CREATE ROLE classpilot WITH LOGIN PASSWORD 'classpilot';"
   sudo -u postgres psql -c "CREATE DATABASE classpilot OWNER classpilot;"
   sudo -u postgres psql -c "CREATE DATABASE classpilot_test OWNER classpilot;"

   # Generate an encryption key for stored Google refresh tokens —
   # use a DIFFERENT key per environment, never commit a real one
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Add the resulting values to `.env`:
   ```
   DATABASE_URL=postgresql://classpilot:classpilot@localhost:5432/classpilot
   TOKEN_ENCRYPTION_KEY=<the key printed above>
   ```

   Then create the tables (idempotent — safe to run anytime, including on
   every app startup):
   ```bash
   python -c "from classpilot.db import init_schema; init_schema()"
   ```

   To run the Postgres-backed test suite against the separate test
   database instead of your dev one:
   ```bash
   TEST_DATABASE_URL=postgresql://classpilot:classpilot@localhost:5432/classpilot_test \
     python -m pytest tests/test_user_store.py -v
   ```

5. **First run — authenticate**
   ```bash
   classpilot-ai-mcp
   ```
   A browser window opens for Google login on first run; a token is saved
   to `GOOGLE_TOKEN_PATH` (default `token.json`) for future runs.

   > If you've used an older version of this project before the current
   > scope set (see "Google OAuth scopes" above) was adopted, delete
   > `token.json` once and re-authenticate so Google issues a token that
   > matches exactly — old tokens authorized for now-dropped scopes
   > (full `drive`, `documents`, etc.) keep working but are broader than
   > what the app requests today, and won't automatically narrow on their own.

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
| `DATABASE_URL` | `postgresql://classpilot:classpilot@localhost:5432/classpilot` | Postgres connection string for the multi-user persistence foundation (Phase 1) — not required for today's single-user flow, see "Database setup" above |
| `TOKEN_ENCRYPTION_KEY` | — | Fernet key encrypting stored Google refresh tokens; required only by code that actually calls `classpilot.crypto`/`classpilot.user_store`, not by the app's current live auth path |

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

**One exception:** `tests/test_user_store.py` runs real integration tests
against a live Postgres database (see "Database setup" above) — since the
whole point of that module is proving the schema/encryption/CRUD actually
work, not just that the SQL looks plausible. It skips gracefully (not a
failure) if no Postgres is reachable, so `pytest tests/` still passes
cleanly without one set up:

```bash
# without Postgres running: 302 passed, 46 skipped
# with Postgres running:    348 passed
```

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

**`Failed to exchange authorization code: (invalid_grant) Missing code verifier`**
Fixed as of this version — this was a real bug where the PKCE
`code_verifier` generated at `/auth/google/login` never reached
`/auth/google/callback` (each leg built its own, unrelated
`google_auth_oauthlib.flow.Flow` instance). If you still see this: make
sure you're running the current `oauth_web.py`/`google_oauth.py`/
`oauth_state.py`, and that your `oauth_states` table has a `code_verifier`
column (`init_schema()` migrates an existing table automatically — run
`python -c "from classpilot.db import init_schema; init_schema()"` once
if you set up Phase 2 before this fix).

**The callback shows broader scopes than expected (e.g. full `drive`,
`classroom.coursework.students`, `classroom.rosters.readonly`)**
This is Google's own `include_granted_scopes=true` behavior, not a bug in
what ClassPilot requests — the actual authorization URL only ever asks for
the curated scope list (see "Google OAuth scopes" above; verified by
`tests/test_google_oauth.py::TestRequestedScopesMatchCuratedSet`). If your
Google account (or this Google Cloud project) previously granted broader
scopes — e.g. from testing before the Phase 1 scope audit, or via other
OAuth clients in the same project — Google will report those as still
"granted" alongside the new request. ClassPilot handles this correctly
(see `tests/test_google_oauth.py::TestGrantedScopeSupersetHandling`): it
relaxes the underlying OAuth library's overly strict scope-match check for
supersets specifically, while independently verifying every scope it
actually requires was still granted — a response *missing* a required
scope still fails authorization. To get a truly minimal grant instead of
a superset, revoke ClassPilot's access at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions)
and re-authorize from a clean state.

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

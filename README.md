<div align="center">

# ClassPilot AI <img src="assets/classpilot-logo.png" alt="ClassPilot AI Logo" width="30" height="30" align="center" style="vertical-align: middle;" />

### Your Google Classroom. One AI Conversation.

[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![Protocol](https://img.shields.io/badge/Protocol-Model%20Context%20Protocol%20(MCP)-blueviolet?style=flat)](https://modelcontextprotocol.io/)
[![Framework](https://img.shields.io/badge/Server-FastMCP%203.x-6C5CE7?style=flat)](https://github.com/jlowin/fastmcp)
[![Database](https://img.shields.io/badge/Database-PostgreSQL%20%7C%20Neon-4169E1?style=flat&logo=postgresql&logoColor=white)](https://neon.tech/)
[![Google API](https://img.shields.io/badge/Google%20APIs-Classroom%20%26%20Drive-EA4335?style=flat&logo=google&logoColor=white)](https://developers.google.com/classroom)
[![Security](https://img.shields.io/badge/Auth-OAuth%202.1%20%2B%20PKCE-brightgreen?style=flat)](docs/ARCHITECTURE.md)
[![Encryption](https://img.shields.io/badge/Tokens-Fernet%20AES--128--CBC-success?style=flat)](docs/PRIVACY.md)
[![CI Tests](https://img.shields.io/badge/Tests-430%2B%20Passing-success?style=flat&logo=githubactions&logoColor=white)](.github/workflows/classpilot-full-tests.yml)
[![Website](https://img.shields.io/badge/Website-classpilot--ai.floot.app-FF3B30?style=flat&logo=googlechrome&logoColor=white)](https://classpilot-ai.floot.app)

<p align="center">
  <b>Production FastMCP server bridging ChatGPT & Claude directly to Google Classroom.</b><br>
  Engineered with OAuth 2.1 + PKCE, visual slide diagram extraction, encrypted credential persistence, and multi-tenant isolation.
</p>

<p align="center">
  <a href="https://classpilot-ai.floot.app"><b>🌐 Visit Website</b></a> &nbsp;•&nbsp;
  <a href="https://classpilot-ai.onrender.com/auth/google/login"><b>🔗 Connect Google Classroom</b></a>
</p>

[Visit Website](https://classpilot-ai.floot.app) • [Connect Google Classroom](https://classpilot-ai.onrender.com/auth/google/login) • [Live Website](#live-website) • [Quick Start](#quick-start-guide-for-hosted-users) • [Architecture](#architecture) • [Six MCP Tools](#six-mcp-tools--example-queries) • [Visual Extraction](#supported-document-formats--visual-extraction) • [Documentation](docs/)

</div>

---

## Overview

**ClassPilot AI** transforms static learning management systems into an interactive, conversational study workspace. Built natively on the open [Model Context Protocol (MCP)](https://modelcontextprotocol.io), ClassPilot allows AI assistants (such as ChatGPT with Developer Mode, Claude Desktop, and Cursor) to securely query a student's Google Classroom courses on demand.

Beyond simple metadata listing, ClassPilot performs **deep in-memory content and visual extraction**. It extracts structured text from slide presentations, documents, spreadsheets, and PDFs, while extracting diagrams, charts, and architectural figures as high-resolution visual blocks for vision-capable AI models.

ClassPilot AI is built with an enterprise-grade security posture: multi-tenant PostgreSQL persistence, at-rest Fernet token encryption, SHA-256 hashed bearer tokens, and strict separation between MCP client authentication and Google OAuth credentials.

The standalone public landing page is now live at [https://classpilot-ai.floot.app](https://classpilot-ai.floot.app).

---

## Live Website

ClassPilot AI has a lightweight public landing page explaining the project, features, architecture, security, and onboarding flow.

* **Website**: [https://classpilot-ai.floot.app](https://classpilot-ai.floot.app)
* **Google Classroom Connection**: [https://classpilot-ai.onrender.com/auth/google/login](https://classpilot-ai.onrender.com/auth/google/login)
* **Hosted MCP Endpoint**: `https://classpilot-ai.onrender.com/mcp`

---

## The Problem ClassPilot AI Solves

Modern academic learning environments are fragmented across folders, announcements, and attachments:

* **Information Fragmentation**: Syllabi, lecture notes, problem sets, and announcements are buried inside deep Classroom hierarchies and Google Drive folders.
* **Context Blindness in LLMs**: General-purpose AI assistants have no real-time awareness of a student's enrolled courses, instructor expectations, or approaching deadlines.
* **Visual Context Loss**: Crucial educational diagrams (system architectures, mathematical plots, biochemical pathways, flowcharts) in slide decks and PDFs are completely invisible to text-only scrapers.
* **Security & Multi-Tenancy Risks**: Exposing personal Google credentials directly to third-party AI prompts creates unacceptable privacy risks and violates tenant boundaries.

ClassPilot AI solves these challenges by providing a dedicated, read-only MCP interface that resolves student identity server-side, streams files securely in memory, extracts both text and visual diagrams, and protects credentials behind hardened OAuth 2.1 boundaries.

---

## Key Features & Engineering Highlights

* **Native Model Context Protocol (MCP) Interface**: Exposes standardized tools over Streamable HTTP (for web clients like ChatGPT) and stdio (for local environments like Claude Desktop).
* **Deep Multimodal Document Extraction**: Automatically extracts text from Google Docs, Slides, Sheets, `.pptx`, `.pdf`, and `.docx`, while intelligently surfacing embedded diagrams and charts.
* **Visual Significance Heuristics**: Custom algorithms detect diagram-only slides and large visual figures while automatically filtering out repeating header logos, watermarks, and icons.
* **Dual-OAuth Domain Isolation**: Strictly isolates MCP client authentication (OAuth 2.1 + S256 PKCE) from Google OAuth. Google tokens are **never** shared with or accessible by the connected AI.
* **At-Rest Fernet Token Encryption**: All stored Google refresh tokens are encrypted using AES-128-CBC with HMAC-SHA256 authentication. MCP tokens are stored strictly as SHA-256 hashes.
* **Ephemeral In-Memory Processing**: Drive attachments are streamed into memory (`io.BytesIO`) during tool execution and never written to the server filesystem.
* **On-Demand Deadline Prioritization**: Calculates remaining minutes and hours for upcoming assignments across all enrolled courses without running battery-draining background daemons.

---

## Watch ClassPilot AI in Action

<!-- DEMO VIDEO SECTION: Replace this card with the screen recording embed when available -->
> ### 🎥 Demonstration Walkthrough Coming Soon
>
> A comprehensive video demonstration showing ClassPilot AI in action—including Google Classroom account linking, ChatGPT custom connector configuration, real-time syllabus navigation, and visual diagram inspection from lecture slides—will be linked here shortly.
>
> ```markdown
> <!-- Insert video walkthrough link below -->
> [![ClassPilot AI Demo Video](docs/assets/demo-thumbnail.png)](https://www.youtube.com/watch?v=YOUR_VIDEO_ID)
> ```

---

## Architecture

ClassPilot AI implements a dual-domain security architecture. The AI client communicates exclusively through the ClassPilot FastMCP server using ClassPilot-issued Bearer tokens, while the server coordinates with Google APIs on behalf of authenticated students using encrypted refresh tokens.

```mermaid
flowchart TD
    subgraph Clients ["AI Client Layer"]
        AI["ChatGPT / Claude / Cursor / MCP Client"]
    end

    subgraph MCPAuthLayer ["MCP Authentication & Identity Boundary"]
        MCPOAuth["ClassPilot OAuth 2.1 Server<br/>(RFC 8414 Discovery & S256 PKCE)"]
        TokenVer["ClassPilot TokenVerifier<br/>(SHA-256 Hashed Tokens)"]
        IdentRes["Identity Resolution Seam<br/>resolve_identity()"]
    end

    subgraph ServerCore ["ClassPilot FastMCP Server Core"]
        FastMCP["FastMCP Server (Streamable HTTP / stdio)"]
        Tools["6 Focused MCP Tools<br/>• list_classes • list_modules<br/>• list_materials • get_material<br/>• search_classroom • get_upcoming_deadlines"]
        DocEngine["Document & Visual Extraction Engine<br/>(pypdf, python-pptx, python-docx, Pillow)"]
    end

    subgraph GoogleAuthLayer ["Google OAuth & Credentials Boundary"]
        GoogleOAuthWeb["Google Web OAuth Flow<br/>(/auth/google/login & /callback)"]
        CredManager["Credential Manager<br/>get_authorized_credentials()"]
        Crypto["Fernet Encryption Engine<br/>(AES-128-CBC + HMAC-SHA256)"]
    end

    subgraph Persistence ["Persistence Layer (PostgreSQL / Neon)"]
        UserDB[("users<br/>(UUID, Google sub, Email)")]
        CredDB[("google_oauth_credentials<br/>(Encrypted Refresh Tokens)")]
        TokenDB[("mcp_tokens<br/>(SHA-256 Hashed Bearer Tokens)")]
    end

    subgraph GoogleAPIs ["Google Cloud APIs"]
        ClassroomAPI["Google Classroom API v1<br/>(Courses, Topics, Materials, Due Dates)"]
        DriveAPI["Google Drive API v3<br/>(Binary Attachments & Workspace Exports)"]
    end

    %% Client Interactions
    AI -->|"1. OAuth 2.1 Handshake (Code + PKCE)"| MCPOAuth
    MCPOAuth -->|"Store Hashed Bearer Token"| TokenDB
    AI -->|"2. Tool Invocation (Bearer Token)"| FastMCP

    %% FastMCP & Identity Resolution
    FastMCP -->|"Validate Token"| TokenVer
    TokenVer -->|"Verify Hash"| TokenDB
    TokenVer -->|"Resolve Caller"| IdentRes
    IdentRes -->|"Extract verified user_id"| FastMCP

    %% Google OAuth Setup
    GoogleOAuthWeb -->|"User Login & Consent"| GoogleAPIs
    GoogleOAuthWeb -->|"Encrypt Refresh Token"| Crypto
    Crypto -->|"Persist Encrypted Token"| CredDB
    GoogleOAuthWeb -->|"Record User Profile"| UserDB

    %% Tool Execution & Google APIs
    FastMCP -->|"Dispatch Tool"| Tools
    Tools -->|"Request Authorized Credentials"| CredManager
    CredManager -->|"Retrieve Encrypted Token"| CredDB
    CredManager -->|"Decrypt Token"| Crypto
    CredManager -->|"Auto-refreshed Credentials"| Tools
    Tools -->|"Query Course Metadata"| ClassroomAPI
    Tools -->|"Download In-Memory Streams"| DriveAPI
    DriveAPI -->|"Attachment Stream (io.BytesIO)"| DocEngine
    DocEngine -->|"Text & Image Content Blocks"| Tools
    Tools -->|"3. Structured JSON + Visual Blocks"| AI

    %% Styling
    classDef client fill:#e1f5fe,stroke:#0288d1,stroke-width:2px,color:#01579b;
    classDef mcp fill:#ede7f6,stroke:#7e57c2,stroke-width:2px,color:#311b92;
    classDef core fill:#e8f5e9,stroke:#43a047,stroke-width:2px,color:#1b5e20;
    classDef google fill:#fff3e0,stroke:#fb8c00,stroke-width:2px,color:#e65100;
    classDef db fill:#fce4ec,stroke:#d81b60,stroke-width:2px,color:#880e4f;
    classDef ext fill:#fffde7,stroke:#fbc02d,stroke-width:2px,color:#f57f17;

    class AI client;
    class MCPOAuth,TokenVer,IdentRes mcp;
    class FastMCP,Tools,DocEngine core;
    class GoogleOAuthWeb,CredManager,Crypto google;
    class UserDB,CredDB,TokenDB db;
    class ClassroomAPI,DriveAPI ext;
```

For comprehensive architectural design decisions, sequence flows, and threat models, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Six MCP Tools & Example Queries

ClassPilot AI exposes exactly six focused, read-only tools designed to support academic inquiry while eliminating risks associated with assignment modification or unintended side effects:

| Tool | Parameters | Description |
|---|---|---|
| [`list_classes`](classpilot/server.py#L267) | *None* | Lists all active Google Classroom courses in which the student is enrolled. |
| [`list_modules`](classpilot/server.py#L284) | `course_id: str` | Lists syllabus topics/modules inside a specific course in structured sequence. |
| [`list_materials`](classpilot/server.py#L307) | `course_id: str`, `topic_id: str?`, `include_assignments: bool`, `include_announcements: bool` | Lists lecture materials, assignment postings, and announcements for a module or course. |
| [`get_material`](classpilot/server.py#L386) | `course_id: str`, `material_id: str`, `material_kind: str`, `read_attachments: bool` | Retrieves material content, extracting full attachment text and embedded diagrams/charts. |
| [`search_classroom`](classpilot/server.py#L497) | `query: str` | Performs global text search across course names, module titles, and material descriptions. |
| [`get_upcoming_deadlines`](classpilot/server.py#L533) | `within_hours: int` (default `168`) | Queries coursework across all enrolled classes and returns deadlines sorted by proximity. |

### Example Conversational Prompts

* **Discovering Courses**:
  > *"What courses am I enrolled in this semester, and what are their course IDs?"*
* **Navigating Course Syllabi**:
  > *"What topics are covered in my CS 440 Distributed Systems class?"*
* **Reviewing Materials**:
  > *"List all lecture slides and readings posted under Module 3 in Machine Learning."*
* **Diagram-Aware Study Assistance**:
  > *"Examine the lecture slides for Week 5 in Cloud Computing. Summarize the Paxos consensus diagram shown on slide 8 and explain the leader election phase."*
* **Cross-Course Research**:
  > *"Search all my classes for any mention of 'Fourier Transform' and tell me which lecture files discuss it."*
* **Proactive Deadline Management**:
  > *"What assignments are due across all my courses within the next 48 hours, and how many hours remain for each?"*

---

## How the Project Works

1. **Authentication & Identity Threading**:
   When an AI client issues an MCP tool call over Streamable HTTP, it provides a Bearer token. ClassPilot's [`TokenVerifier`](classpilot/mcp_auth.py) verifies the SHA-256 token hash against PostgreSQL and maps it to a verified `user_id`. Identity is never passed as a client-controlled tool argument.
2. **Credential Resolution**:
   The internal [`get_authorized_credentials()`](classpilot/google_oauth.py) engine retrieves the student's Fernet-encrypted Google refresh token from PostgreSQL, decrypts it in memory, and checks token expiration. If expired, it transparently requests a refreshed access token from Google Identity Services.
3. **Classroom & Drive Data Retrieval**:
   [`classpilot/study_client.py`](classpilot/study_client.py) queries the Google Classroom API v1 using the refreshed credentials, automatically following `nextPageToken` cursors so large courses are never truncated.
4. **Multimodal Content Extraction**:
   Drive attachments are downloaded into an in-memory buffer (`io.BytesIO`). Text extractors parse the document structure, while [`classpilot/visual_extractor.py`](classpilot/visual_extractor.py) filters and extracts high-resolution diagram images.
5. **Structured MCP Response**:
   The FastMCP server packages the result into a structured `MaterialContent` JSON block alongside native `Image` content blocks, providing the AI with immediate text and visual comprehension.

---

## Supported Document Formats & Visual Extraction

ClassPilot extracts readable text and visual figures from all primary educational document formats:

| Format | Text Content | Visual Figures & Diagrams | Extraction Technology |
|---|:---:|:---:|---|
| **Google Docs** | ✅ Full Text | — | Drive `export_media` (`text/plain`) |
| **Google Slides** | ✅ All Slides | — | Drive `export_media` (`text/plain`) |
| **Google Sheets** | ✅ Tabular CSV | — | Drive `export_media` (`text/csv`) |
| **Plain Text (`.txt`, `.md`, `.json`, `.csv`)** | ✅ Full Content | — | Direct Drive download (`utf-8`) |
| **PowerPoint (`.pptx`)** | ✅ Slide text, tables & notes | ✅ Embedded diagrams & charts | `python-pptx` |
| **Adobe PDF (`.pdf`)** | ✅ Page-by-page text | ✅ Visual figures & plots | `pypdf` |
| **Word (`.docx`)** | ✅ Paragraphs & tables | ✅ Embedded figures | `python-docx` |
| **Direct Images (`.png`, `.jpg`, `.jpeg`)** | — | ✅ Full image pass-through | Direct Drive download |
| **Legacy Binary (`.ppt`, `.doc`)** | ⚠️ Link & title only | — | Legacy binary; direct web link returned |
| **YouTube & Web Links** | ⚠️ Link & title only | — | External URLs returned without scraping |

### Visual Extraction Significance Heuristics

Rather than running slow, inaccurate OCR on server instances, ClassPilot surfaces raw image bytes directly to multimodal AI models. To preserve context limits and eliminate noise:

1. **Area Ratio Threshold**: Standard embedded images must occupy at least 5% of the slide or page area (`MIN_AREA_RATIO = 0.05`). On **diagram-only slides** (pages with zero extractable text), the threshold is relaxed to 2% (`MIN_DIAGRAM_ONLY_AREA_RATIO = 0.02`).
2. **Watermark & Logo Deduplication**: The SHA-256 byte hash of each candidate image is computed. Any image appearing across 3 or more slides (`REPEATED_IMAGE_MIN_COUNT = 3`) is flagged as a header logo, template background, or watermark and discarded.
3. **Response Capping**: A strict limit of **8 images per file** and **8 images per response** prevents bloated payloads. Diagram-only slides receive top ranking priority.

For further implementation details, see [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md).

---

## Quick-Start Guide for Hosted Users

Connecting your Google Classroom to an AI client using an already deployed ClassPilot instance requires two straightforward steps:

### Step 1: Connect Your Google Account
1. Open your web browser and navigate to the hosted login page:
   ```text
   https://classpilot-ai.onrender.com/auth/google/login
   ```
   (Alternatively, visit the [Live Website](https://classpilot-ai.floot.app) and click **Connect Google Classroom**.)
2. Sign in with your Google account and grant the requested read-only Classroom and Drive permissions.
3. When the browser confirms **"Connected!"**, your account is registered.

### Step 2: Add Custom Connector in Your AI Client

#### In ChatGPT (Plus / Team / Enterprise)
1. In ChatGPT, open **Settings → Connected Apps / Connectors → Advanced**.
2. Toggle on **Developer Mode** and click **Add Custom Connector**.
3. Enter your server URL:
   ```text
   https://classpilot-ai.onrender.com/mcp
   ```
4. Select **OAuth**. ChatGPT will discover the RFC 8414 metadata, dynamically register, and execute the PKCE authorization flow.

#### In Claude Desktop
Add ClassPilot to your `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "classpilot": {
      "url": "https://classpilot-ai.onrender.com/mcp"
    }
  }
}
```

#### In Cursor
Add the server entry to `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "classpilot": {
      "url": "https://classpilot-ai.onrender.com/mcp"
    }
  }
}
```

---

## Local Development & Self-Hosting Instructions

### 1. Prerequisites
* Python 3.10+ (Python 3.11 recommended)
* PostgreSQL 14+ (or a cloud PostgreSQL instance like [Neon](https://neon.tech))
* Google Cloud Console project with **Google Classroom API** and **Google Drive API** enabled

### 2. Installation
```bash
# Clone the repository
git clone git@github.com:Rama-chandra55/classpilot-ai.git
cd classpilot-ai

# Create and activate virtual environment
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate
# On Windows:
.venv\Scripts\Activate.ps1

# Install in editable mode
pip install --upgrade pip
pip install -e .
```

### 3. Environment Configuration
Copy the configuration template:
```bash
cp .env.example .env
```
Generate an encryption key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Populate `.env` with your values:
```dotenv
DATABASE_URL=postgresql://user:password@localhost:5432/classpilot
TOKEN_ENCRYPTION_KEY=<generated-fernet-key>
LLM_API_KEY=<your-api-key>
LLM_PROVIDER=gemini
LLM_MODEL=gemini-2.5-flash-lite

GOOGLE_WEB_CREDENTIALS_PATH=google_web_credentials.json
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback

MCP_AUTH_MODE=remote
MCP_PUBLIC_BASE_URL=http://localhost:8000
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
MCP_HTTP_PATH=/mcp
```

### 4. Initialize Database Schema
```bash
python -c "from classpilot.db import init_schema; init_schema()"
```

### 5. Run the Server
Launch the unified Streamable HTTP server:
```bash
classpilot-ai-http
# or:
python -m classpilot.http_server
```
The server will be available at `http://127.0.0.1:8000`:
- **MCP Endpoint**: `http://127.0.0.1:8000/mcp`
- **Google OAuth Login**: `http://127.0.0.1:8000/auth/google/login`
- **OAuth Discovery**: `http://127.0.0.1:8000/.well-known/oauth-authorization-server`
- **Health Checks**: `http://127.0.0.1:8000/health` (liveness) & `/ready` (readiness)

For full step-by-step installation instructions, consult [`docs/SETUP.md`](docs/SETUP.md).

---

## Security, Authentication & User Isolation

ClassPilot AI is built upon strict security and privacy standards:

* **Separation of OAuth Realms**: Google tokens and MCP Bearer tokens inhabit completely distinct security realms. The connected AI model never handles Google tokens.
* **At-Rest Fernet Encryption**: Google OAuth refresh tokens are encrypted using AES-128-CBC authenticated with HMAC-SHA256 (`cryptography.fernet`).
* **SHA-256 Hashed MCP Tokens**: MCP client Bearer tokens are stored only as one-way SHA-256 hashes in PostgreSQL.
* **PKCE S256 Enforcement**: The MCP OAuth 2.1 implementation requires RFC 7636 PKCE with `code_challenge_method=S256`. Insecure `plain` challenges are rejected.
* **In-Flight Callback Coalescing**: A thread-safe locking mechanism prevents duplicate code exchange attempts caused by aggressive browser pre-fetching.
* **Zero Disk Storage of Files**: Documents and lecture slides are streamed directly into memory buffers (`io.BytesIO`) during tool execution and discarded immediately after.

For our full privacy specification and audited scope breakdown, refer to [`docs/PRIVACY.md`](docs/PRIVACY.md).

---

## Tech Stack

* **Core Language**: Python 3.11+
* **MCP Framework**: [FastMCP 3.x](https://github.com/jlowin/fastmcp) (Streamable HTTP & stdio)
* **Web & Async Foundation**: Starlette, Uvicorn, AnyIO
* **Database & Pooling**: PostgreSQL, [Neon Serverless](https://neon.tech/), `psycopg[binary]`, `psycopg-pool`
* **Cryptography & Security**: `cryptography` (Fernet), hashlib (SHA-256), PKCE (RFC 7636)
* **Google APIs**: Google Classroom API v1 (`google-api-python-client`), Google Drive API v3
* **Document Processing**: `pypdf`, `python-pptx`, `python-docx`, `Pillow`
* **LLM Engine Compatibility**: Google Gemini (`google-genai`), Groq (`groq`), Anthropic (`anthropic`)
* **Testing & Quality Assurance**: `pytest`, `pytest-asyncio`, `pytest-timeout`, `reportlab`

---

## Repository Structure

```text
classpilot-ai/
├── classpilot/                     # Core application package
│   ├── classroom_client.py         # Curated Google Classroom client & scope bounds
│   ├── config.py                   # Centralized environment configuration
│   ├── crypto.py                   # Fernet AES-128 token encryption engine
│   ├── db.py                       # PostgreSQL connection pooling & schema initialization
│   ├── google_oauth.py             # Google OAuth flow & proactive token refresh
│   ├── http_server.py              # Streamable HTTP MCP server entry point
│   ├── identity.py                 # Per-request caller identity resolution seam
│   ├── mcp_auth.py                 # RFC 9728 TokenVerifier & MCP token lifecycle
│   ├── mcp_oauth_web.py            # OAuth 2.1 + PKCE authorization server endpoints
│   ├── oauth_state.py              # Single-use CSRF & PKCE state persistence
│   ├── oauth_web.py                # Google account linking Starlette application
│   ├── server.py                   # FastMCP server exposing the 6 active tools
│   ├── study_client.py             # Classroom/Drive API retrieval & document parsers
│   ├── user_store.py               # User and credential relational persistence
│   └── visual_extractor.py         # Diagram & chart extraction pipeline
├── docs/                           # Technical documentation suite
│   ├── ARCHITECTURE.md             # System topology, sequence flows, and threat model
│   ├── CAPABILITIES.md             # Document parsing matrix & visual heuristics
│   ├── PRIVACY.md                  # Privacy architecture & audited OAuth scopes
│   └── SETUP.md                    # Installation, GCP configuration, and client setup
├── tests/                          # Automated test suite (430+ unit & integration tests)
├── vendor_classroom_suite_mcp/     # Vendored base classroom client (MIT licensed)
├── .env.example                    # Clean environment configuration template
├── .gitignore                      # Git exclusion rules
├── CONTRIBUTING.md                 # Contributor guide & development workflow
├── Dockerfile                      # Production container definition
├── pyproject.toml                  # Python package configuration & dependencies
├── README.md                       # Product overview & documentation index
└── SECURITY.md                     # Vulnerability reporting & security policy
```

---

## Testing & Quality Assurance

ClassPilot AI maintains a comprehensive suite of **over 430 unit and integration tests** validating tool behaviors, authentication boundaries, document extraction heuristics, and error recovery:

```bash
# Execute the complete test suite
pytest

# Run tests with verbose output
pytest -v

# Run core unit tests in isolation
pytest tests/test_study_tools.py tests/test_visual_extractor.py tests/test_google_oauth.py -v
```

All external dependencies (Google APIs, FastMCP, and LLM providers) are stubbed during unit test execution, allowing the suite to run reliably without network credentials.

Continuous Integration is automated via GitHub Actions in [`.github/workflows/classpilot-full-tests.yml`](.github/workflows/classpilot-full-tests.yml).

---

## Limitations & Project Scope

* **Read-Only Operation**: ClassPilot AI does not support assignment submission, grade modification, or course enrollment.
* **Server-Side OCR**: Scanned, image-only PDFs with no embedded text layer will not have their text extracted on the server; the original image must be processed via the AI client's vision model.
* **Legacy Microsoft Office Formats**: Pre-2007 binary formats (`.doc`, `.ppt`) cannot be parsed into text. Direct Drive links are returned instead.
* **External Media**: Transcripts from YouTube video links and external web URLs posted in announcements are not scraped or transcribed.

---

## Contributing

We welcome community contributions! Please review [`CONTRIBUTING.md`](CONTRIBUTING.md) for details on code style, branch management, architectural guidelines, and test requirements before submitting a pull request.

---

## License & Acknowledgments

* **ClassPilot AI**: Developed and maintained by [Rama-chandra55](https://github.com/Rama-chandra55).
* **Vendored Components**: Includes `vendor_classroom_suite_mcp`, distributed under the MIT License (see [`vendor_classroom_suite_mcp/LICENSE`](vendor_classroom_suite_mcp/LICENSE)).
* **Special Thanks**: Built on the open [Model Context Protocol](https://modelcontextprotocol.io) and [FastMCP](https://github.com/jlowin/fastmcp).

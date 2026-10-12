# ClassPilot AI — Technical Architecture

ClassPilot AI is a production-grade [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that enables AI clients (such as ChatGPT, Claude, and Cursor) to securely query Google Classroom courses, modules, announcements, and course materials, with deep text and visual extraction.

---

## 1. System Topology & Dual-OAuth Architecture

A core architectural principle of ClassPilot AI is the **strict separation between Google authentication and MCP client authentication**. These two authorization boundaries operate independently and are never conflated:

```
┌─────────────────────────────────┐
│     AI Client / Assistant       │
│  (ChatGPT / Claude / Cursor)    │
└────────────────┬────────────────┘
                 │ 1. Bearer <ClassPilot MCP Token> (OAuth 2.1 + PKCE)
                 ▼
┌────────────────────────────────────────────────────────┐
│             ClassPilot AI Application                   │
│                                                        │
│  ┌──────────────────────┐    ┌──────────────────────┐  │
│  │   FastMCP Server     │    │   OAuth 2.1 Server   │  │
│  │   (Streamable HTTP)  │    │   (/mcp/authorize)   │  │
│  └──────────┬───────────┘    └──────────▲───────────┘  │
│             │                           │              │
│             ▼                           │              │
│  ┌──────────────────────────────────────┴───────────┐  │
│  │  Identity Seam: resolve_identity()               │  │
│  │  - Maps MCP Bearer token hash to users.id        │  │
│  │  - Fails closed if token is invalid or expired   │  │
│  └──────────────────┬───────────────────────────────┘  │
│                     │                                  │
│                     ▼                                  │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Credential Engine: get_authorized_credentials() │  │
│  │  - Decrypts at-rest Google refresh token         │  │
│  │  - Automatically refreshes access token if due   │  │
│  └──────────────────┬───────────────────────────────┘  │
└─────────────────────┼──────────────────────────────────┘
                      │
                      │ 2. Authenticated Google API Request
                      ▼
┌────────────────────────────────────────────────────────┐
│               Google Cloud Platform                    │
│                                                        │
│   ┌─────────────────────┐    ┌─────────────────────┐   │
│   │ Google Classroom API│    │  Google Drive API   │   │
│   │ (Courses/Materials) │    │  (Files/Export/Doc) │   │
│   └─────────────────────┘    └─────────────────────┘   │
└────────────────────────────────────────────────────────┘
```

### Why Dual OAuth?
1. **Zero Credential Leakage**: An AI client (e.g., ChatGPT) never receives, sees, or stores Google OAuth access or refresh tokens. It receives only an opaque ClassPilot-issued Bearer token.
2. **Independent Revocation**: A student can revoke an AI client's access without invalidating their Google connection, or revoke Google access to immediately neutralize all connected AI sessions.
3. **Multi-Tenant Security**: Identity is resolved server-side from request headers. The calling model cannot forge or specify a target user ID.

---

## 2. Component Architecture

```mermaid
graph TD
    subgraph Client ["Client Layer"]
        LLM["AI Client (ChatGPT / Claude / Cursor)"]
        Browser["User Web Browser"]
    end

    subgraph Transport ["Transport & Routing Layer"]
        HTTPServer["HTTP Server (Starlette / Uvicorn)"]
        FastMCPEndpoint["Streamable HTTP MCP (/mcp)"]
        OAuthWebRoutes["OAuth Web Routes (/auth/google/*, /mcp/*)"]
    end

    subgraph Auth ["Security & Identity Layer"]
        TokenVerifier["ClassPilotTokenVerifier"]
        IdentitySeam["resolve_identity()"]
        Crypto["Fernet Token Encryption (AES-128-CBC)"]
        GoogleOAuthEngine["Google OAuth Engine & Caching"]
    end

    subgraph Storage ["Persistence Layer (PostgreSQL)"]
        UserTable[("users")]
        CredTable[("google_oauth_credentials")]
        MCPTokenTable[("mcp_tokens")]
        OAuthStateTable[("oauth_states")]
    end

    subgraph Core ["Execution & Extraction Layer"]
        StudyClient["Study Client (Classroom & Drive APIs)"]
        VisualExtractor["Visual Extractor (PPTX / PDF / DOCX)"]
    end

    subgraph External ["Google APIs"]
        ClassroomAPI["Google Classroom API v1"]
        DriveAPI["Google Drive API v3"]
    end

    Browser -->|1. Connect Account| OAuthWebRoutes
    OAuthWebRoutes -->|Store Credentials| Crypto
    Crypto --> CredTable
    OAuthWebRoutes --> UserTable

    LLM -->|2. MCP Tools + Bearer Token| FastMCPEndpoint
    FastMCPEndpoint --> TokenVerifier
    TokenVerifier --> MCPTokenTable
    TokenVerifier --> IdentitySeam
    IdentitySeam --> GoogleOAuthEngine
    GoogleOAuthEngine --> CredTable
    GoogleOAuthEngine --> Crypto
    
    FastMCPEndpoint --> StudyClient
    StudyClient --> ClassroomAPI
    StudyClient --> DriveAPI
    DriveAPI --> VisualExtractor
    StudyClient -->|Results + Visual Blocks| LLM
```

---

## 3. End-to-End Sequence Workflows

### Flow A: Google Account Linking (User Onboarding)
Before an MCP client can access Classroom resources, the user establishes a link between their Google account and ClassPilot AI:

```mermaid
sequenceDiagram
    autonumber
    actor User as Student
    participant Web as ClassPilot Web Server
    participant DB as PostgreSQL
    participant Google as Google Identity Services

    User->>Web: GET /auth/google/login
    Web->>Web: Generate PKCE verifier + CSRF state token
    Web->>DB: INSERT into oauth_states (state, code_verifier)
    Web-->>User: 302 Redirect to Google OAuth Consent Screen
    User->>Google: Authenticate & Grant Scopes
    Google-->>User: 302 Redirect to /auth/google/callback?code=...&state=...
    User->>Web: GET /auth/google/callback
    Web->>DB: Atomically consume state & fetch code_verifier
    Web->>Google: Exchange authorization code with PKCE verifier
    Google-->>Web: ID Token, Access Token, Refresh Token
    Web->>Web: Verify Google 'sub' (stable user ID)
    Web->>Web: Encrypt Refresh Token via Fernet (AES-128-CBC)
    Web->>DB: UPSERT user into users & credentials into google_oauth_credentials
    Web-->>User: 200 HTML "Connected! ClassPilot is now connected."
```

### Flow B: MCP Client OAuth 2.1 Authorization
When ChatGPT, Claude, or Cursor connects to ClassPilot AI:

```mermaid
sequenceDiagram
    autonumber
    participant Client as MCP Client (e.g. ChatGPT)
    participant Auth as ClassPilot OAuth 2.1 Server
    participant DB as PostgreSQL

    Client->>Auth: GET /.well-known/oauth-authorization-server
    Auth-->>Client: RFC 8414 Discovery Metadata (endpoints, S256 PKCE)
    Client->>Auth: POST /mcp/register (RFC 7591 Dynamic Client Registration)
    Auth->>DB: INSERT into mcp_clients
    Auth-->>Client: client_id, client_secret
    Client->>Auth: GET /mcp/authorize?response_type=code&code_challenge=...&login_hint=user@domain.com
    Auth->>DB: Verify user exists and has connected Google credentials
    Auth->>DB: Store authorization code + PKCE challenge (TTL = 5 min)
    Auth-->>Client: 302 Redirect with code
    Client->>Auth: POST /mcp/token (code + code_verifier)
    Auth->>Auth: Verify S256 PKCE challenge against code_verifier
    Auth->>Auth: Generate opaque MCP access & refresh tokens
    Auth->>DB: Store SHA-256 token hashes (never plaintext)
    Auth-->>Client: 200 JSON { access_token, refresh_token, token_type: Bearer }
```

### Flow C: Tool Execution & Identity Threading
When an AI client calls one of the six MCP tools:

```mermaid
sequenceDiagram
    autonumber
    participant Client as AI Client
    participant MCP as FastMCP Server
    participant Auth as TokenVerifier & Identity
    participant GoogleAuth as GoogleOAuth Engine
    participant Extractor as Study Client & Visual Extractor
    participant Google as Google APIs

    Client->>MCP: POST /mcp (Call tool: get_material) + Bearer Token
    MCP->>Auth: Verify Bearer token (SHA-256 lookup in DB)
    Auth->>Auth: resolve_identity() -> user_id
    MCP->>GoogleAuth: get_authorized_credentials(user_id)
    GoogleAuth->>GoogleAuth: Decrypt Fernet refresh token & refresh access token if needed
    GoogleAuth-->>MCP: Live google.oauth2.credentials.Credentials
    MCP->>Extractor: get_material(course_id, material_id)
    Extractor->>Google: Classroom API: fetch material metadata
    Extractor->>Google: Drive API: download attachment stream (in-memory)
    Extractor->>Extractor: Extract text + parse significant visual diagrams
    Extractor-->>MCP: MaterialContent (JSON) + ImageContent blocks
    MCP-->>Client: MCP Result with structured data and visuals
```

---

## 4. Key Security Mechanisms

### A. Custom FastMCP TokenVerifier
Rather than relying on FastMCP's generic `OAuthProxy` component (which has historically been affected by vulnerabilities such as CVE-2025-69196, CVE-2026-27124, and AIKIDO-2026-10735), ClassPilot AI implements a purpose-built `ClassPilotTokenVerifier` conforming directly to FastMCP's `RemoteAuthProvider` and `TokenVerifier` interfaces.

### B. At-Rest Token Encryption & Hashing
- **Google Refresh Tokens**: Encrypted using Fernet symmetric encryption (`cryptography.fernet.Fernet`), combining 128-bit AES in CBC mode with PKCS7 padding and HMAC-SHA256 authentication. A database dump never yields plaintext Google tokens.
- **MCP Access & Refresh Tokens**: Hashed using SHA-256 before storage in PostgreSQL (`mcp_tokens.token_hash`). Bearer tokens presented in HTTP headers are hashed on the fly for comparison.

### C. Thread-Safe In-Flight OAuth Coalescing
Modern browsers often trigger pre-fetches or speculative navigation on OAuth redirects, causing race conditions with single-use authorization codes. ClassPilot AI handles this via `_inflight_callbacks` with a `threading.Lock()` and `threading.Event()`, coalescing concurrent callback requests for the same authorization state without replay risks.

### D. Multi-Tenant Physical Data Isolation
All persistent records (`users`, `google_oauth_credentials`, `mcp_tokens`, `oauth_states`) enforce foreign key constraints anchored on verified `users.id` UUIDs. Data queries strictly filter by the authenticated user's ID, guaranteeing zero cross-tenant contamination.

### E. Ephemeral In-Memory Document Streaming
When attachments (PDFs, PPTX, DOCX, Docs) are downloaded from Google Drive, they are streamed directly into an in-memory `io.BytesIO` buffer. Extracted text and image bytes are returned in the MCP response and immediately garbage-collected. **No student files, attachments, or personal study notes are ever written to the server's disk.**

# ClassPilot AI — Technical Privacy & Data-Handling Overview

This document provides a factual, technical specification of how ClassPilot AI handles user data, Google account credentials, course materials, and AI communication.

---

## 1. Architectural Privacy Principles

1. **Principle of Least Privilege**: ClassPilot requests only read-only scopes necessary to enumerate courses and read materials. No administrative or write scopes are ever requested.
2. **Zero Plaintext Tokens**: All Google refresh tokens are encrypted at rest using AES-128-CBC with HMAC-SHA256 authentication. All MCP tokens are hashed using SHA-256.
3. **No Credential Exposure to LLMs**: Neither Google access tokens nor Google refresh tokens are ever sent to, exposed to, or accessible by connected AI clients (e.g. ChatGPT, Claude, Cursor).
4. **Ephemeral In-Memory Document Streaming**: Attachment files downloaded from Google Drive exist exclusively within ephemeral in-memory byte buffers (`io.BytesIO`). **No student files, slide decks, or assignments are ever written to the server's disk or stored in the database.**
5. **Physical Multi-Tenant Data Isolation**: Database queries strictly filter by the authenticated user's unique identity. Cross-user data leakage is prevented at the schema and query layer.

---

## 2. Data Storage Specification

ClassPilot stores minimal state in a relational database (PostgreSQL / Neon) strictly required for authentication and session management.

### What Is Stored
| Table | Fields Stored | Purpose & Protection |
|---|---|---|
| `users` | `id` (UUID), `google_sub` (unique ID), `email`, `display_name`, `created_at` | Maps a stable Google identity to a ClassPilot tenant ID. |
| `google_oauth_credentials` | `user_id`, `encrypted_refresh_token`, `access_token`, `expiry`, `scopes` | Enables automated token refreshing. Refresh tokens are **encrypted at rest** using Fernet symmetric encryption. |
| `mcp_clients` | `client_id`, `client_name`, `redirect_uris`, `created_at` | Stores registered MCP clients (RFC 7591 dynamic client registration). |
| `mcp_tokens` | `token_hash`, `client_id`, `user_id`, `expires_at`, `scopes` | Authenticates MCP requests. Bearer tokens are stored **only as SHA-256 hashes**. |
| `oauth_states` | `state`, `code_verifier`, `expires_at` | Ephemeral state tokens for CSRF prevention and PKCE verification. Automatically consumed upon login. |

### What Is NEVER Stored
- **Course Documents & Attachments**: PDFs, presentations, documents, and images are streamed into memory for the duration of the tool execution and immediately discarded.
- **Student Work & Grades**: ClassPilot AI does not record grades, class rosters, or submitted homework.
- **AI Chat Logs**: Conversation histories between the student and the AI client are held exclusively by the AI client (ChatGPT, Claude, Cursor); ClassPilot operates strictly as a stateless tool execution server.

---

## 3. Audited Google OAuth Scopes

ClassPilot AI requests an audited, minimal set of read-only Google scopes:

| Scope | Technical Justification |
|---|---|
| `classroom.courses.readonly` | Enables `list_classes` to display courses the user is enrolled in. |
| `classroom.coursework.me` | Enables `get_upcoming_deadlines` and assignment listings for the current student. |
| `classroom.courseworkmaterials` | Enables `list_materials` to enumerate lecture materials and handouts. |
| `classroom.announcements` | Enables `list_materials` to read instructor announcements. |
| `classroom.topics.readonly` | Enables `list_modules` to organize courses into their syllabus topics. |
| `drive.readonly` | Enables `get_material` to download and read course file attachments and export Google Docs/Slides/Sheets. |

### Scopes Explicitly Excluded
- ❌ `classroom.coursework.students` — Teacher-only scope granting access to all student submissions; excluded.
- ❌ `classroom.rosters.readonly` — Roster and student email inspection; excluded.
- ❌ `drive` (Read/Write) — Write access to student Google Drive; excluded in favor of `drive.readonly`.
- ❌ `documents` (Google Docs API) — Full Docs API access; excluded in favor of standard Drive export.

---

## 4. Encryption & Security Standards

- **At-Rest Token Encryption**: Fernet (`cryptography.fernet.Fernet`) using 128-bit AES in CBC mode with an HMAC-SHA256 signature for integrity. Encryption keys are supplied via `TOKEN_ENCRYPTION_KEY` and are never committed to version control.
- **In-Transit Security**: All client-to-server and server-to-Google communications require TLS 1.3/1.2 over HTTPS.
- **PKCE Verification**: Authorization code exchanges on the MCP OAuth surface strictly enforce RFC 7636 PKCE with `code_challenge_method=S256`. Plain PKCE challenges are rejected.

---

## 5. User Revocation & Account Deletion

Users retain complete control over their credentials:
1. **Google Side**: Revoking access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) instantly invalidates ClassPilot's ability to refresh access tokens, immediately disabling all connected tools.
2. **MCP Side**: Calling the `/mcp/revoke` endpoint (or disconnecting the connector in ChatGPT/Claude) revokes the ClassPilot Bearer token.

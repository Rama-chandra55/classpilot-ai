# ClassPilot AI — Setup & Deployment Guide

This guide covers everything required to configure, run, and self-host ClassPilot AI, from Google Cloud Console configuration and PostgreSQL database setup to AI client connection (ChatGPT, Claude, and Cursor).

---

## 1. Prerequisites

Before getting started, ensure you have:

- **Python 3.10+** (Python 3.11 recommended)
- **PostgreSQL 14+** (a local instance or a managed cloud database such as [Neon](https://neon.tech))
- **Google Cloud Platform (GCP) Account** with administrative access to create an OAuth 2.0 Web Application
- **LLM API Key** from [Google AI Studio](https://aistudio.google.com/) (Gemini free tier recommended), [Groq](https://console.groq.com), or [Anthropic](https://console.anthropic.com)
- **MCP Client** (ChatGPT Plus/Team/Enterprise with Developer Mode, Claude Desktop / claude.ai, or Cursor)

---

## 2. Google Cloud Platform Configuration

ClassPilot AI uses the official Google Classroom API and Google Drive API with a minimal, audited set of read-only permissions.

### A. Create Project & Enable APIs
1. Navigate to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (e.g., `classpilot-ai-prod`).
3. In the sidebar, go to **APIs & Services → Library**.
4. Search for and enable the following APIs:
   - **Google Classroom API**
   - **Google Drive API**

### B. Configure OAuth Consent Screen
1. Go to **APIs & Services → OAuth consent screen**.
2. Select User Type:
   - **External** (standard for personal and educational testing accounts).
   - **Internal** (if deploying strictly within a Google Workspace organization).
3. Fill in mandatory fields: App name (`ClassPilot AI`), User support email, and Developer contact information.
4. Click **Save and Continue** to reach the **Scopes** page.
5. Click **Add or Remove Scopes** and select the following curated scopes:
   - `https://www.googleapis.com/auth/classroom.courses.readonly`
   - `https://www.googleapis.com/auth/classroom.coursework.me`
   - `https://www.googleapis.com/auth/classroom.courseworkmaterials`
   - `https://www.googleapis.com/auth/classroom.announcements`
   - `https://www.googleapis.com/auth/classroom.topics.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`
6. Under **Test Users** (if Publishing Status is "Testing"):
   - Add every Google account email address that will connect to your instance.

### C. Create OAuth 2.0 Web Application Credentials
1. Go to **APIs & Services → Credentials**.
2. Click **Create Credentials → OAuth client ID**.
3. Select Application type: **Web application**.
4. Name: `ClassPilot Web Client`.
5. Under **Authorized redirect URIs**, add the callback URI for your deployment:
   - Local development: `http://localhost:8000/auth/google/callback`
   - Production / Hosted: `https://<your-domain>/auth/google/callback`
6. Click **Create**.
7. Download the credentials JSON and save it in the repository root as `google_web_credentials.json` (do **never** commit this file).

---

## 3. Database & Secret Configuration

ClassPilot AI uses PostgreSQL for multi-tenant user persistence, session management, and encrypted credential storage.

### A. Provision PostgreSQL
You can run PostgreSQL locally or provision a free serverless tier on [Neon](https://neon.tech).

Obtain your connection string:
```text
postgresql://user:password@ep-sample-pool.us-east-2.aws.neon.tech/classpilot?sslmode=require
```

### B. Generate Token Encryption Key
ClassPilot encrypts stored Google OAuth refresh tokens at rest using AES-128-CBC + HMAC-SHA256 (Fernet). Generate a cryptographically secure key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save the resulting 44-character urlsafe base64 string.

---

## 4. Local Installation & Configuration

### A. Clone Repository & Setup Virtual Environment
```bash
git clone git@github.com:Rama-chandra55/classpilot-ai.git
cd classpilot-ai

python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

### B. Install Dependencies
```bash
pip install --upgrade pip
pip install -e .
```

### C. Environment Variables (.env)
Copy the example configuration:
```bash
cp .env.example .env
```

Populate the following required variables:
```dotenv
DATABASE_URL=postgresql://user:password@localhost:5432/classpilot
TOKEN_ENCRYPTION_KEY=<your-44-char-fernet-key>
LLM_API_KEY=<your-gemini-or-groq-api-key>
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

### D. Initialize Database Schema
Run the idempotent schema initializer:
```bash
python -c "from classpilot.db import init_schema; init_schema()"
```
This sets up tables: `users`, `google_oauth_credentials`, `oauth_states`, `mcp_clients`, `mcp_tokens`, and `mcp_authorization_codes`.

---

## 5. Running the Server

### Mode 1: Unified Streamable HTTP Server (Recommended)
This runs the production-grade HTTP server serving both the FastMCP endpoint and OAuth endpoints on a single origin:
```bash
classpilot-ai-http
# or:
python -m classpilot.http_server
```
The server binds to `http://127.0.0.1:8000` (or `0.0.0.0:$PORT` when deployed).
- MCP Endpoint: `http://127.0.0.1:8000/mcp`
- Google Account Link: `http://127.0.0.1:8000/auth/google/login`
- OAuth Discovery: `http://127.0.0.1:8000/.well-known/oauth-authorization-server`
- Health Checks: `http://127.0.0.1:8000/health` (liveness), `http://127.0.0.1:8000/ready` (readiness)

### Mode 2: Local Stdio Mode (Claude Desktop Local Testing)
For local stdio testing without remote HTTP networking:
1. In `.env`, set:
   ```dotenv
   MCP_AUTH_MODE=local_dev
   CLASSPILOT_DEV_USER_ID=<user-uuid-from-database>
   ```
2. Run via:
   ```bash
   classpilot-ai-mcp
   ```

---

## 6. Connecting AI Clients

### Step 1: Link Google Account
Before connecting an AI client in `remote` mode, the user links their Google Classroom account:
1. Visit `http://localhost:8000/auth/google/login` (or hosted `https://<your-domain>/auth/google/login`) in a web browser.
2. Complete Google sign-in and grant requested scopes.
3. A confirmation screen ("Connected!") indicates the account is stored securely.

### Step 2: Configure AI Client

#### ChatGPT (Custom Connector)
1. Ensure you have an active ChatGPT Plus, Team, or Enterprise subscription.
2. Go to **Settings → Connected Apps / Connectors → Advanced → Enable Developer Mode**.
3. Click **Add Custom Connector**.
4. Enter Server URL: `https://<your-domain>/mcp`
5. Select **OAuth** authentication.
6. ChatGPT uses RFC 8414 discovery to dynamically register via `/mcp/register` and authenticate via OAuth 2.1 + PKCE.

#### Claude (claude.ai & Claude Desktop)
- **claude.ai**: Go to **Settings → Connectors → Add Custom Connector** and input your server URL (`https://<your-domain>/mcp`).
- **Claude Desktop (stdio mode)**: Edit `claude_desktop_config.json`:
  ```json
  {
    "mcpServers": {
      "classpilot": {
        "command": "python",
        "args": ["-m", "classpilot.server"],
        "env": {
          "MCP_AUTH_MODE": "local_dev",
          "CLASSPILOT_DEV_USER_ID": "<your-user-uuid>",
          "DATABASE_URL": "postgresql://...",
          "TOKEN_ENCRYPTION_KEY": "...",
          "LLM_API_KEY": "..."
        }
      }
    }
  }
  ```

#### Cursor
Add the server configuration to `.cursor/mcp.json` (workspace or global):
```json
{
  "mcpServers": {
    "classpilot": {
      "url": "https://<your-domain>/mcp"
    }
  }
}
```

---

## 7. Production Deployment (e.g. Render, Railway, Cloud Containers)

When hosting ClassPilot AI in production:

1. **Docker Container**:
   Build and run using the root `Dockerfile`:
   ```bash
   docker build -t classpilot-ai .
   docker run -p 8080:8080 --env-file .env classpilot-ai
   ```
2. **Platform Deployment (Render)**:
   - Environment: Python 3.11 / Web Service
   - Build Command: `pip install .`
   - Start Command: `python -m classpilot.http_server`
   - Port: Render injects `$PORT`, which `classpilot.http_server` automatically reads and binds to `0.0.0.0:$PORT`.
3. **Production Verification**:
   - Verify health check: `curl https://<your-domain>/health` → `{"status": "ok"}`
   - Verify readiness check: `curl https://<your-domain>/ready` → `{"status": "ready"}`
   - Verify OAuth discovery: `curl https://<your-domain>/.well-known/oauth-authorization-server`

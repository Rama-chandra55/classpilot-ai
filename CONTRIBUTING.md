# Contributing to ClassPilot AI

Thank you for your interest in contributing to ClassPilot AI! We welcome contributions that improve reliability, performance, document parsing fidelity, and developer experience.

---

## 1. Development Principles & Architectural Constraints

When submitting contributions, please adhere to these core principles:

1. **Security & OAuth Boundary Integrity**:
   - The separation between Google OAuth (`/auth/google/*`) and MCP client OAuth (`/mcp/*`) is absolute. No code changes may pass Google tokens across MCP boundaries or expose them to LLM responses.
   - At-rest token encryption (`classpilot.crypto`) and SHA-256 token hashing must not be bypassed or relaxed.
2. **Focused Six-Tool Surface**:
   - ClassPilot AI exposes exactly six read-only tools: `list_classes`, `list_modules`, `list_materials`, `get_material`, `search_classroom`, and `get_upcoming_deadlines`.
   - Do not reintroduce background watcher daemons, assignment submission handlers, or automated email notification services.
3. **In-Memory Streaming**:
   - Document parsers must process files in memory (`io.BytesIO`). Do not write student documents or attachments to the local filesystem.
4. **Zero Untested Dependencies**:
   - Keep dependencies lean. Avoid introducing heavy native dependencies unless strictly necessary.

---

## 2. Local Setup

### Prerequisites
- Python 3.10+ (Python 3.11 recommended)
- PostgreSQL 14+ (or a free [Neon](https://neon.tech) database)
- Git

### Initializing Environment
```bash
# Clone the repository
git clone git@github.com:Rama-chandra55/classpilot-ai.git
cd classpilot-ai

# Create a virtual environment
python -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

# Install development dependencies and package in editable mode
pip install --upgrade pip
pip install -e .
pip install pytest pytest-timeout reportlab
```

### Environment Configuration
```bash
cp .env.example .env
```
Fill in test credentials in `.env`. Never commit `.env` or any credential files (`credentials.json`, `token.json`, `google_web_credentials.json`).

---

## 3. Running the Test Suite

All contributions must pass the test suite:

```bash
# Run the complete test suite
pytest

# Run tests with verbose output
pytest -v

# Run unit tests only (no PostgreSQL connection needed)
pytest tests/test_study_tools.py tests/test_visual_extractor.py tests/test_google_oauth.py -v
```

> **Note on Integration Tests**: Tests in `tests/test_user_store.py` and `tests/test_mcp_auth.py` run against a live PostgreSQL database if `TEST_DATABASE_URL` is set, and skip gracefully if no database is connected.

---

## 4. Submitting a Pull Request

1. **Create a Feature Branch**:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. **Commit Changes**:
   - Write clear, descriptive commit messages adhering to standard conventional commit conventions (e.g., `feat:`, `fix:`, `docs:`, `refactor:`).
3. **Verify Documentation**:
   - If modifying document extraction, update [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md).
   - If adding or adjusting configuration parameters, update [`docs/SETUP.md`](docs/SETUP.md) and [`.env.example`](.env.example).
4. **Push & Open PR**:
   - Push to your fork and open a Pull Request against the `main` branch.

"""
ClassPilot AI Configuration

All environment-driven settings live here. This is the only file that should
read os.environ directly - everything else receives a ClassPilotConfig object.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ClassPilotConfig:
    # --- LLM (provider-agnostic) ---
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-4-6"))

    # --- Watcher ---
    watch_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("WATCH_INTERVAL_MINUTES", "15"))
    )

    # --- Deadline alert offsets (minutes before due time) ---
    deadline_alert_offsets_minutes: tuple = (24 * 60, 6 * 60, 60, 15)

    # --- State storage (SQLite — watcher/deadline dedup cache; unrelated to
    #     the new Postgres-backed user/credential store below) ---
    state_db_path: str = field(
        default_factory=lambda: os.getenv("STATE_DB_PATH", "classpilot_state.db")
    )

    # --- Multi-user persistence foundation (Phase 1) ---
    # Postgres holds `users` and their encrypted Google OAuth credentials.
    # Not yet wired into the live single-user auth flow (vendor auth.py /
    # token.json) — this is the storage substrate Phase 2's web OAuth flow
    # will write to and read from.
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://classpilot:classpilot@localhost:5432/classpilot"
        )
    )
    # Fernet key (44-char urlsafe-base64 string from Fernet.generate_key()).
    # No default — classpilot/crypto.py raises a clear error if a caller
    # actually tries to encrypt/decrypt without one configured, rather than
    # silently using a weak or predictable key.
    token_encryption_key: str = field(
        default_factory=lambda: os.getenv("TOKEN_ENCRYPTION_KEY", "")
    )

    # --- Google web OAuth flow (Phase 2) ---
    # A SEPARATE OAuth client from GOOGLE_CREDENTIALS_PATH above: Google
    # requires a "Web application" type client for a server-side redirect
    # flow (the existing credentials.json is a "Desktop app" client used
    # only by the InstalledAppFlow fallback). Keeping them as two distinct
    # files/client IDs means the fallback flow genuinely can't be broken
    # by anything in this phase.
    google_web_credentials_path: str = field(
        default_factory=lambda: os.getenv(
            "GOOGLE_WEB_CREDENTIALS_PATH", "google_web_credentials.json"
        )
    )
    google_oauth_redirect_uri: str = field(
        default_factory=lambda: os.getenv(
            "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8090/auth/google/callback"
        )
    )
    oauth_web_host: str = field(
        default_factory=lambda: os.getenv("OAUTH_WEB_HOST", "127.0.0.1")
    )
    oauth_web_port: int = field(
        default_factory=lambda: int(os.getenv("OAUTH_WEB_PORT", "8090"))
    )

    # --- Notifications ---
    notifier_backend: str = field(default_factory=lambda: os.getenv("NOTIFIER_BACKEND", "email"))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    notify_to_email: str = field(default_factory=lambda: os.getenv("NOTIFY_TO_EMAIL", ""))

    # --- HTTP MCP transport (Streamable HTTP for remote clients) ---
    mcp_http_host: str = field(
        default_factory=lambda: os.getenv("MCP_HTTP_HOST", "127.0.0.1")
    )
    mcp_http_port: int = field(
        default_factory=lambda: int(os.getenv("MCP_HTTP_PORT", "8000"))
    )
    mcp_http_path: str = field(
        default_factory=lambda: os.getenv("MCP_HTTP_PATH", "/mcp")
    )

    # --- Logging ---
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def validate(self) -> None:
        """Raise a clear error early if required settings are missing."""
        if not self.llm_api_key:
            raise ValueError(
                "LLM_API_KEY is not set. Add it to your .env file "
                "(this is the API key for whichever LLM_PROVIDER you configured)."
            )


_config: "ClassPilotConfig | None" = None


def get_config() -> ClassPilotConfig:
    """Return the process-wide config singleton."""
    global _config
    if _config is None:
        _config = ClassPilotConfig()
    return _config

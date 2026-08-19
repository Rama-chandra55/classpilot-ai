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

    # --- State storage ---
    state_db_path: str = field(
        default_factory=lambda: os.getenv("STATE_DB_PATH", "classpilot_state.db")
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

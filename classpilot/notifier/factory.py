"""
Notifier Factory

Maps configuration -> concrete Notifier, mirroring classpilot/llm/factory.py.
Adding WhatsApp later: write WhatsAppNotifier(Notifier), register it below,
set NOTIFIER_BACKEND=whatsapp. Nothing else in the app changes.
"""

import logging
from typing import Dict, Optional, Type

from .base import Notifier, NotifierError
from .email_notifier import EmailNotifier

logger = logging.getLogger(__name__)

_NOTIFIER_REGISTRY: Dict[str, Type[Notifier]] = {
    "email": EmailNotifier,
    # "whatsapp": WhatsAppNotifier,   # future
}


def create_notifier(config, to_email_override: Optional[str] = None) -> Notifier:
    """
    Build the configured Notifier instance.

    Args:
        config: ClassPilotConfig instance (see classpilot.config).
        to_email_override: Phase 3 — when provided, sends to this address
            instead of config.notify_to_email. Used by AppServices to
            route a PostgreSQL-identified user's watcher notifications to
            their OWN stored Google account email (see
            classpilot/services.py) rather than one global recipient.
            Leave unset for the legacy standalone path (build_services()
            in services.py), which has no per-user email on file and
            keeps using config.notify_to_email exactly as before Phase 3.

    Returns:
        A ready-to-use Notifier.

    Raises:
        NotifierError: if the configured backend is unknown or misconfigured.
    """
    name = config.notifier_backend.lower()
    notifier_cls = _NOTIFIER_REGISTRY.get(name)

    if notifier_cls is None:
        available = ", ".join(sorted(_NOTIFIER_REGISTRY))
        raise NotifierError(
            f"Unknown notifier backend '{name}'. Available backends: {available}"
        )

    logger.info("Initializing notifier backend: %s", name)

    if name == "email":
        return EmailNotifier(
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_user=config.smtp_user,
            smtp_password=config.smtp_password,
            to_email=to_email_override or config.notify_to_email,
        )

    raise NotifierError(f"Backend '{name}' is registered but not wired up.")

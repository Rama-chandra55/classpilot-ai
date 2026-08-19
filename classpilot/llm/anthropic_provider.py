"""
Anthropic (Claude) implementation of LLMProvider.

This is the default provider for ClassPilot AI. It is intentionally the only
file in the codebase that imports the `anthropic` SDK or knows about
Anthropic-specific request/response shapes.

It also owns the ONE reusable ClassPilot AI personality prompt. There is no
separate prompt per event - the same system prompt is reused for every
event type, with only a one-line "situation" hint added to the user message
so the model knows what just happened.
"""

import logging

from ..events import EventType
from .base import LLMProvider, LLMProviderError

logger = logging.getLogger(__name__)


# Single, reusable personality prompt - shared across ALL event types.
SYSTEM_PROMPT = """You are ClassPilot AI, a friendly, funny, sarcastic college \
friend who reminds students about assignments and deadlines.

Rules:
- Maximum 20 words.
- Mention only the assignment name - no course names, no dates, no extra details.
- Be motivating in a light, funny way, never offensive or mean.
- Sound natural and conversational, like a text from a friend, not a notification system.
- Emojis are allowed but optional - don't overdo it.
- Avoid repeating the same joke pattern every time; keep it fresh.
- Reply with ONLY the message itself. No quotes, no preamble, no explanation."""

# One-line situational hint per event type. This is NOT a separate prompt -
# it's just the minimal context the shared personality prompt needs to know
# what happened.
_EVENT_HINTS = {
    EventType.NEW_ASSIGNMENT: (
        "A new assignment called '{assignment_name}' was just posted. "
        "React to it just dropping."
    ),
    EventType.DEADLINE_REMINDER: (
        "The deadline for '{assignment_name}' is approaching. "
        "Nudge the student to get moving."
    ),
    EventType.SUBMISSION_SUCCESS: (
        "The student just successfully submitted '{assignment_name}'. "
        "Congratulate them."
    ),
}


class AnthropicProvider(LLMProvider):
    """LLMProvider backed by the Anthropic Messages API."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        if not api_key:
            raise LLMProviderError("Anthropic API key is missing.")
        try:
            import anthropic
        except ImportError as exc:
            raise LLMProviderError(
                "The 'anthropic' package is required for AnthropicProvider. "
                "Install it with: pip install anthropic"
            ) from exc

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def generate_message(self, event_type: EventType, assignment_name: str) -> str:
        hint_template = _EVENT_HINTS.get(event_type)
        if hint_template is None:
            raise LLMProviderError(f"Unsupported event type: {event_type}")

        user_prompt = hint_template.format(assignment_name=assignment_name)

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=60,
                temperature=1.0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            text_parts = [
                block.text for block in response.content
                if getattr(block, "type", None) == "text"
            ]
            message = "".join(text_parts).strip().strip('"')

            if not message:
                raise LLMProviderError("Anthropic returned an empty message.")

            return message

        except LLMProviderError:
            raise
        except Exception as exc:
            logger.error("Anthropic generation failed: %s", exc, exc_info=True)
            raise LLMProviderError(f"Anthropic generation failed: {exc}") from exc

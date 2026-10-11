"""
Google Gemini implementation of LLMProvider.

Uses the current google-genai Python SDK.
Free tier: generous daily limits via Google AI Studio.

Get a free API key at: https://aistudio.google.com/app/apikey
Set: LLM_PROVIDER=gemini, LLM_API_KEY=your-key, LLM_MODEL=gemini-2.5-flash-lite
"""

import logging
import re
from typing import Optional

from .base import LLMProvider, LLMProviderError
from .anthropic_provider import SYSTEM_PROMPT, _EVENT_HINTS
from ..events import EventType

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash-lite"

# Models that use thinking tokens (like Gemini 2.5) consume tokens during reasoning.
# Setting max_output_tokens=1000 ensures sufficient budget for thinking tokens
# while keeping generated notification text concise.
DEFAULT_MAX_OUTPUT_TOKENS = 1000


def _sanitize_error_message(msg: str) -> str:
    """Redact sensitive API keys or credentials from error messages."""
    # Redact query params e.g. ?key=... or &key=...
    msg = re.sub(r'([?&]key=)[^&\s]+', r'\1[REDACTED]', msg)
    # Redact Google AI Studio API keys (standard AIza... format)
    msg = re.sub(r'AIza[0-9A-Za-z-_]{35}', '[REDACTED_API_KEY]', msg)
    # Redact Bearer tokens
    msg = re.sub(r'Bearer\s+[A-Za-z0-9._~+/-]+', 'Bearer [REDACTED]', msg)
    return msg


class GeminiProvider(LLMProvider):
    """LLMProvider backed by Google Gemini via the google-genai SDK."""

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ):
        if not api_key:
            raise LLMProviderError("Gemini API key is missing.")

        try:
            from google import genai
            from google.genai import types, errors  # noqa: F401
        except ImportError as exc:
            raise LLMProviderError(
                "The 'google-genai' package is required for GeminiProvider. "
                "Install it with: pip install google-genai"
            ) from exc

        # Discard empty string or default anthropic model if mispassed
        chosen_model = model.strip() if model and model.strip() else DEFAULT_MODEL
        if chosen_model == "claude-sonnet-4-6":
            chosen_model = DEFAULT_MODEL

        self._model = chosen_model
        self._max_output_tokens = max_output_tokens
        self._client = genai.Client(api_key=api_key)

    def __repr__(self) -> str:
        # Never expose API key in string representations
        return f"GeminiProvider(model={self._model!r}, max_output_tokens={self._max_output_tokens})"

    def generate_message(self, event_type: EventType, assignment_name: str) -> str:
        hint_template = _EVENT_HINTS.get(event_type)
        if hint_template is None:
            raise LLMProviderError(f"Unsupported event type: {event_type}")

        user_prompt = hint_template.format(assignment_name=assignment_name)

        try:
            from google.genai import types

            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=1.0,
                max_output_tokens=self._max_output_tokens,
            )

            response = self._client.models.generate_content(
                model=self._model,
                contents=user_prompt,
                config=config,
            )

            text = response.text if response and hasattr(response, "text") else None
            message = (text or "").strip().strip('"')
            if not message:
                raise LLMProviderError("Gemini returned an empty message.")
            return message

        except LLMProviderError:
            raise
        except Exception as exc:
            sanitized_exc_str = _sanitize_error_message(str(exc))
            # Log error without assignment name, user prompt, or API key
            logger.error("Gemini generation failed: %s", sanitized_exc_str)

            code = getattr(exc, "code", None)
            # Check for rate limit / quota exhaustion (HTTP 429)
            if code == 429 or "RESOURCE_EXHAUSTED" in sanitized_exc_str or "rate limit" in sanitized_exc_str.lower():
                raise LLMProviderError(
                    f"Gemini API rate limit exceeded: {sanitized_exc_str}"
                ) from exc

            # Check for model not found / unavailable (HTTP 404 / 503)
            if code == 404 or "NOT_FOUND" in sanitized_exc_str or "not found" in sanitized_exc_str.lower():
                raise LLMProviderError(
                    f"Gemini model '{self._model}' is unavailable or not found: {sanitized_exc_str}"
                ) from exc

            # General API or network error
            raise LLMProviderError(
                f"Gemini generation failed: {sanitized_exc_str}"
            ) from exc

    def generate_test_notification(
        self,
        course_name: str,
        assignment_title: str,
        due_date: Optional[str] = None,
    ) -> str:
        """
        Generate a short, professional assignment notification for test emails.
        Contains the course name, assignment title, and due date when available.
        """
        due_info = f"Due date: {due_date}." if due_date else "No due date specified."
        system_instruction = (
            "You are ClassPilot AI, an intelligent academic assistant. "
            "Generate a short, professional, and clear assignment notification for a student. "
            "Must mention the course name, assignment title, and due date. "
            "Keep the message under 40 words. Do not use quotes or introductory filler."
        )
        user_prompt = (
            f"Course: {course_name}\n"
            f"Assignment: {assignment_title}\n"
            f"{due_info}\n"
        )
        try:
            from google.genai import types

            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7,
                max_output_tokens=self._max_output_tokens,
            )

            response = self._client.models.generate_content(
                model=self._model,
                contents=user_prompt,
                config=config,
            )

            text = response.text if response and hasattr(response, "text") else None
            message = (text or "").strip().strip('"')
            if not message:
                raise LLMProviderError("Gemini returned an empty message.")
            return message

        except LLMProviderError:
            raise
        except Exception as exc:
            sanitized_exc_str = _sanitize_error_message(str(exc))
            logger.error("Gemini test notification generation failed: %s", sanitized_exc_str)
            code = getattr(exc, "code", None)
            if code == 429 or "RESOURCE_EXHAUSTED" in sanitized_exc_str or "rate limit" in sanitized_exc_str.lower():
                raise LLMProviderError(f"Gemini API rate limit exceeded: {sanitized_exc_str}") from exc
            if code == 404 or "NOT_FOUND" in sanitized_exc_str or "not found" in sanitized_exc_str.lower():
                raise LLMProviderError(f"Gemini model '{self._model}' is unavailable or not found: {sanitized_exc_str}") from exc
            raise LLMProviderError(f"Gemini generation failed: {sanitized_exc_str}") from exc


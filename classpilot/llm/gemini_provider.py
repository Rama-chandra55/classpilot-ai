"""
Google Gemini implementation of LLMProvider.

Free tier: 15 requests/minute, 1 million tokens/day.
No credit card required — just a Google account (which you already have).

Get a free API key at: https://aistudio.google.com/app/apikey
Set: LLM_PROVIDER=gemini, LLM_API_KEY=your-key, LLM_MODEL=gemini-1.5-flash
"""

import logging
from typing import Optional

from .base import LLMProvider, LLMProviderError
from .anthropic_provider import SYSTEM_PROMPT, _EVENT_HINTS
from ..events import EventType

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """LLMProvider backed by Google Gemini (free tier via AI Studio)."""

    def __init__(self, api_key: str, model: str = "gemini-1.5-flash"):
        if not api_key:
            raise LLMProviderError("Gemini API key is missing.")
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise LLMProviderError(
                "The 'google-generativeai' package is required for GeminiProvider. "
                "Install it with: pip install google-generativeai"
            ) from exc

        import google.generativeai as genai
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=model,
            system_instruction=SYSTEM_PROMPT,
        )
        self._model_name = model

    def generate_message(self, event_type: EventType, assignment_name: str) -> str:
        hint_template = _EVENT_HINTS.get(event_type)
        if hint_template is None:
            raise LLMProviderError(f"Unsupported event type: {event_type}")

        user_prompt = hint_template.format(assignment_name=assignment_name)

        try:
            response = self._model.generate_content(
                user_prompt,
                generation_config={
                    "max_output_tokens": 60,
                    "temperature": 1.0,
                },
            )
            message = response.text.strip().strip('"')
            if not message:
                raise LLMProviderError("Gemini returned an empty message.")
            return message

        except LLMProviderError:
            raise
        except Exception as exc:
            logger.error("Gemini generation failed: %s", exc, exc_info=True)
            raise LLMProviderError(f"Gemini generation failed: {exc}") from exc

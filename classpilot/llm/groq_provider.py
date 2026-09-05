"""
Groq implementation of LLMProvider.

Free tier: generous daily limits, extremely fast inference.
No credit card required.

Get a free API key at: https://console.groq.com
Set: LLM_PROVIDER=groq, LLM_API_KEY=your-key, LLM_MODEL=llama-3.1-8b-instant
"""

import logging

from .base import LLMProvider, LLMProviderError
from .anthropic_provider import SYSTEM_PROMPT, _EVENT_HINTS
from ..events import EventType

logger = logging.getLogger(__name__)


class GroqProvider(LLMProvider):
    """LLMProvider backed by Groq (free tier)."""

    def __init__(self, api_key: str, model: str = "llama-3.1-8b-instant"):
        if not api_key:
            raise LLMProviderError("Groq API key is missing.")
        try:
            from groq import Groq
        except ImportError as exc:
            raise LLMProviderError(
                "The 'groq' package is required for GroqProvider. "
                "Install it with: pip install groq"
            ) from exc

        from groq import Groq
        self._client = Groq(api_key=api_key)
        self._model = model

    def generate_message(self, event_type: EventType, assignment_name: str) -> str:
        hint_template = _EVENT_HINTS.get(event_type)
        if hint_template is None:
            raise LLMProviderError(f"Unsupported event type: {event_type}")

        user_prompt = hint_template.format(assignment_name=assignment_name)

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=60,
                temperature=1.0,
            )
            message = response.choices[0].message.content.strip().strip('"')
            if not message:
                raise LLMProviderError("Groq returned an empty message.")
            return message

        except LLMProviderError:
            raise
        except Exception as exc:
            logger.error("Groq generation failed: %s", exc, exc_info=True)
            raise LLMProviderError(f"Groq generation failed: {exc}") from exc

"""
LLM Provider Interface

Defines the provider-agnostic contract every LLM backend (Anthropic, OpenAI,
Gemini, Ollama, ...) must implement. Callers only ever know about
`generate_message(event_type, assignment_name)` - they never see raw
prompts, system strings, or vendor-specific request/response shapes. Each
provider implementation owns its own prompt construction internally.
"""

from abc import ABC, abstractmethod

from ..events import EventType


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Concrete implementations (AnthropicProvider, future OpenAIProvider, ...)
    hold the personality system prompt and the per-EventType phrasing
    internally, and return a single ready-to-send message string.
    """

    @abstractmethod
    def generate_message(self, event_type: EventType, assignment_name: str) -> str:
        """
        Generate a short, in-character notification message.

        Args:
            event_type: Which kind of event this message is for.
            assignment_name: The assignment's title, e.g. "DBMS Assignment".

        Returns:
            A single ready-to-send message string (already trimmed/cleaned).

        Raises:
            LLMProviderError: on any failure communicating with the backend.
        """
        raise NotImplementedError


class LLMProviderError(Exception):
    """Raised when an LLM provider fails to generate a response."""
    pass

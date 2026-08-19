"""
LLM Provider Factory

Maps LLM_PROVIDER config value → concrete LLMProvider instance.
Adding a new provider = one new file + one entry in _PROVIDER_REGISTRY.
Nothing else in the codebase needs to change.

FREE providers available out of the box:
  gemini  — Google Gemini (gemini-1.5-flash), free via aistudio.google.com
  groq    — Groq (llama-3.1-8b-instant), free via console.groq.com

Paid provider:
  anthropic — Anthropic Claude, paid via console.anthropic.com
"""

import logging

from .base import LLMProvider, LLMProviderError

logger = logging.getLogger(__name__)


def create_llm_provider(config) -> LLMProvider:
    """
    Build and return the configured LLMProvider.

    Reads config.llm_provider (from LLM_PROVIDER in .env) and imports
    only the matching provider class, so unused provider packages are
    never imported (no ImportError if groq isn't installed when using gemini).
    """
    name = config.llm_provider.lower().strip()
    logger.info("Initializing LLM provider: %s (model=%s)", name, config.llm_model)

    if name == "gemini":
        from .gemini_provider import GeminiProvider
        return GeminiProvider(api_key=config.llm_api_key, model=config.llm_model)

    if name == "groq":
        from .groq_provider import GroqProvider
        return GroqProvider(api_key=config.llm_api_key, model=config.llm_model)

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=config.llm_api_key, model=config.llm_model)

    available = "gemini, groq, anthropic"
    raise LLMProviderError(
        f"Unknown LLM provider '{name}'. Available: {available}\n"
        f"Set LLM_PROVIDER in your .env file."
    )

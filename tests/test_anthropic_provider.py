import sys
import json
import subprocess
import pytest
from unittest.mock import Mock, patch
from classpilot.llm.anthropic_provider import AnthropicProvider, LLMProviderError, SYSTEM_PROMPT
from classpilot.events import EventType

# conftest.py stubs `anthropic`, so we must inspect the real SDK in a subprocess
def get_real_anthropic_kwargs():
    script = (
        "import inspect, json, anthropic; "
        "print(json.dumps(list(inspect.signature(anthropic.Anthropic().messages.create).parameters.keys())))"
    )
    output = subprocess.check_output([sys.executable, "-c", script], text=True)
    return set(json.loads(output))

def get_real_anthropic_version():
    script = "import anthropic; print(anthropic.__version__)"
    return subprocess.check_output([sys.executable, "-c", script], text=True).strip()

def test_anthropic_provider_kwargs_compatible():
    """
    Verify that the kwargs passed to messages.create() are actually supported
    by the currently installed anthropic SDK. This prevents regressions where
    Anthropic removes parameters (like 'temperature' in 1.11.0).
    """
    # 1. Get the allowed kwargs from the real installed SDK
    allowed_kwargs = get_real_anthropic_kwargs()
    anthropic_version = get_real_anthropic_version()
    
    # We must patch the stubbed `anthropic` module's Anthropic class
    with patch("anthropic.Anthropic", create=True) as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        
        mock_block = Mock()
        mock_block.type = "text"
        mock_block.text = "Mocked AI message."
        mock_response = Mock()
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response
        
        provider = AnthropicProvider(api_key="fake-key")
        result = provider.generate_message(EventType.NEW_ASSIGNMENT, "Test Assignment")
        
        # Verify response parsing
        assert result == "Mocked AI message."
        
        # 3. Assert called with correct kwargs
        mock_client.messages.create.assert_called_once()
        _, kwargs = mock_client.messages.create.call_args
        
        # Verify no unsupported kwargs were passed
        for kwarg in kwargs:
            assert kwarg in allowed_kwargs, (
                f"Regression caught: '{kwarg}' is not a valid parameter "
                f"in anthropic SDK version {anthropic_version}."
            )
        
        # Verify our expected kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"
        assert kwargs["max_tokens"] == 60
        assert kwargs["system"] == SYSTEM_PROMPT
        assert len(kwargs["messages"]) == 1
        assert "Test Assignment" in kwargs["messages"][0]["content"]

def test_anthropic_provider_empty_response():
    """Verify behavior when the LLM returns no text blocks."""
    with patch("anthropic.Anthropic", create=True) as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        
        mock_response = Mock()
        mock_response.content = []  # No text blocks
        mock_client.messages.create.return_value = mock_response
        
        provider = AnthropicProvider(api_key="fake-key")
        with pytest.raises(LLMProviderError, match="Anthropic returned an empty message."):
            provider.generate_message(EventType.NEW_ASSIGNMENT, "Test Assignment")

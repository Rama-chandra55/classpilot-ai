import sys
import types
import pytest
from unittest.mock import MagicMock, Mock, patch


class APIError(Exception):
    def __init__(self, code=500, response_json=None, *args, **kwargs):
        super().__init__(f"{code} {response_json}")
        self.code = code
        self.response_json = response_json


class ClientError(APIError):
    pass


class ServerError(APIError):
    pass


class GenerateContentConfig:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _setup_gemini_stubs():
    if "google" not in sys.modules:
        sys.modules["google"] = types.ModuleType("google")

    if "google.genai" not in sys.modules:
        genai_mod = types.ModuleType("google.genai")
        sys.modules["google.genai"] = genai_mod
    else:
        genai_mod = sys.modules["google.genai"]
    setattr(sys.modules["google"], "genai", genai_mod)

    if "google.genai.errors" not in sys.modules:
        errors_mod = types.ModuleType("google.genai.errors")
        sys.modules["google.genai.errors"] = errors_mod
    else:
        errors_mod = sys.modules["google.genai.errors"]
    errors_mod.APIError = APIError
    errors_mod.ClientError = ClientError
    errors_mod.ServerError = ServerError
    setattr(genai_mod, "errors", errors_mod)

    if "google.genai.types" not in sys.modules:
        types_mod = types.ModuleType("google.genai.types")
        sys.modules["google.genai.types"] = types_mod
    else:
        types_mod = sys.modules["google.genai.types"]
    types_mod.GenerateContentConfig = GenerateContentConfig
    setattr(genai_mod, "types", types_mod)

    if not hasattr(genai_mod, "Client"):
        genai_mod.Client = object


_setup_gemini_stubs()

from classpilot.llm.gemini_provider import (
    GeminiProvider,
    LLMProviderError,
    DEFAULT_MODEL,
    DEFAULT_MAX_OUTPUT_TOKENS,
    _sanitize_error_message,
)
from classpilot.llm.anthropic_provider import SYSTEM_PROMPT, _EVENT_HINTS
from classpilot.events import EventType
from classpilot.config import ClassPilotConfig
from classpilot.llm.factory import create_llm_provider


@pytest.fixture(autouse=True)
def ensure_genai_stubs():
    _setup_gemini_stubs()
    yield



def test_gemini_provider_success_all_event_types():
    """Verify GeminiProvider succeeds and formats prompts correctly for all EventTypes."""
    for event_type in EventType:
        with patch("google.genai.Client") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client

            mock_response = Mock()
            mock_response.text = '  "Keep calm and submit the DBMS assignment!"  \n'
            mock_client.models.generate_content.return_value = mock_response

            provider = GeminiProvider(api_key="test-api-key")
            result = provider.generate_message(event_type, "DBMS Assignment")

            # Check quotes and whitespace are trimmed
            assert result == "Keep calm and submit the DBMS assignment!"

            # Verify Client was initialized with the api_key
            mock_client_cls.assert_called_once_with(api_key="test-api-key")

            # Verify generate_content call
            mock_client.models.generate_content.assert_called_once()
            _, kwargs = mock_client.models.generate_content.call_args

            assert kwargs["model"] == DEFAULT_MODEL
            assert "DBMS Assignment" in kwargs["contents"]

            # Verify config
            config = kwargs["config"]
            assert config.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
            assert config.temperature == 1.0
            assert config.system_instruction == SYSTEM_PROMPT


def test_gemini_provider_custom_model_and_tokens():
    """Verify configurable model and output-token budget are respected."""
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client

        mock_response = Mock()
        mock_response.text = "You got this!"
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiProvider(
            api_key="test-key",
            model="gemini-2.5-pro",
            max_output_tokens=750,
        )
        result = provider.generate_message(EventType.NEW_ASSIGNMENT, "Physics 101")
        assert result == "You got this!"

        _, kwargs = mock_client.models.generate_content.call_args
        assert kwargs["model"] == "gemini-2.5-pro"
        assert kwargs["config"].max_output_tokens == 750


def test_gemini_provider_missing_api_key():
    """Verify that a missing API key raises LLMProviderError early."""
    with pytest.raises(LLMProviderError, match="Gemini API key is missing."):
        GeminiProvider(api_key="")


def test_gemini_provider_empty_response():
    """Verify empty response raises LLMProviderError."""
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client

        mock_response = Mock()
        mock_response.text = "   "
        mock_client.models.generate_content.return_value = mock_response

        provider = GeminiProvider(api_key="test-key")
        with pytest.raises(LLMProviderError, match="Gemini returned an empty message."):
            provider.generate_message(EventType.NEW_ASSIGNMENT, "Essay")


def test_gemini_provider_rate_limit_handling():
    """Verify rate limits (HTTP 429 / RESOURCE_EXHAUSTED) are caught and reported clearly."""
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client

        api_err = APIError(429, {"error": {"message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}})
        mock_client.models.generate_content.side_effect = api_err

        provider = GeminiProvider(api_key="test-key")
        with pytest.raises(LLMProviderError, match="Gemini API rate limit exceeded"):
            provider.generate_message(EventType.DEADLINE_REMINDER, "Calculus")


def test_gemini_provider_model_unavailable_handling():
    """Verify unavailable or not found models (HTTP 404 / NOT_FOUND) are reported cleanly."""
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client

        api_err = APIError(404, {"error": {"message": "Model not found", "status": "NOT_FOUND"}})
        mock_client.models.generate_content.side_effect = api_err

        provider = GeminiProvider(api_key="test-key", model="nonexistent-model")
        with pytest.raises(LLMProviderError, match="unavailable or not found"):
            provider.generate_message(EventType.NEW_ASSIGNMENT, "Lab Report")


def test_gemini_provider_generic_api_error():
    """Verify general API failures raise LLMProviderError with descriptive message."""
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client

        api_err = APIError(500, {"error": {"message": "Internal error", "status": "INTERNAL"}})
        mock_client.models.generate_content.side_effect = api_err

        provider = GeminiProvider(api_key="test-key")
        with pytest.raises(LLMProviderError, match="Gemini generation failed"):
            provider.generate_message(EventType.NEW_ASSIGNMENT, "History Essay")


def test_gemini_provider_unsupported_event():
    """Verify unsupported event type raises LLMProviderError."""
    with patch("google.genai.Client"):
        provider = GeminiProvider(api_key="test-key")
        with pytest.raises(LLMProviderError, match="Unsupported event type"):
            provider.generate_message("INVALID_EVENT", "Chemistry")


def test_gemini_provider_never_logs_sensitive_data(caplog):
    """Verify API keys, tokens, and sensitive inputs are never logged."""
    secret_key = "AIzaSySecretApiKey12345678901234567890"
    with patch("google.genai.Client") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client

        # Simulate exception containing the API key in the URL/error text
        mock_client.models.generate_content.side_effect = Exception(
            f"Failed to connect to https://generativelanguage.googleapis.com/v1beta?key={secret_key}"
        )

        provider = GeminiProvider(api_key=secret_key)

        # 1. Ensure __repr__ does not expose the key
        assert secret_key not in repr(provider)

        # 2. Ensure generation error logs do not contain the key
        with pytest.raises(LLMProviderError):
            provider.generate_message(EventType.NEW_ASSIGNMENT, "Top Secret Assignment Title")

        for record in caplog.records:
            assert secret_key not in record.message
            assert "[REDACTED]" in record.message or "[REDACTED_API_KEY]" in record.message


def test_sanitize_error_message_helper():
    """Verify sanitization helper correctly redacts various token/key formats."""
    sample = "Error at https://api.google.com?key=AIzaSyFakeKey1234567890123456789012345 with Bearer ya29.a0AfH6"
    sanitized = _sanitize_error_message(sample)
    assert "AIzaSyFakeKey" not in sanitized
    assert "ya29.a0AfH6" not in sanitized
    assert "[REDACTED]" in sanitized or "[REDACTED_API_KEY]" in sanitized


def test_factory_creates_gemini_provider():
    """Verify create_llm_provider correctly builds GeminiProvider."""
    config = ClassPilotConfig(
        llm_provider="gemini",
        llm_api_key="fake-gemini-key",
        llm_model="gemini-2.5-flash-lite",
    )
    with patch("google.genai.Client"):
        provider = create_llm_provider(config)
        assert isinstance(provider, GeminiProvider)
        assert provider._model == "gemini-2.5-flash-lite"
        assert provider._max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS

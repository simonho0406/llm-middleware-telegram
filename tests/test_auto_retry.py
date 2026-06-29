"""Tests for the auto-retry on LLM error feature."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from bot.response_generator import _generate_llm_response


@pytest.fixture
def mock_context():
    """Create a mock context with user_data and chat_data."""
    context = MagicMock()
    context.user_data = {}
    context.chat_data = {}
    # Provide a proper async mcp_service so the new tool-fetching path works.
    mock_mcp = AsyncMock()
    mock_mcp.get_all_tools = AsyncMock(return_value=[])
    # Provide a stub skill_service so the lazy-init path (which reads config) is bypassed.
    mock_skills = MagicMock()
    mock_skills.get_skills_as_tools = MagicMock(return_value=[])
    context.application = MagicMock()
    context.application.bot_data = {
        'mcp_service': mock_mcp,
        'skill_service': mock_skills,
    }
    return context


@pytest.fixture
def mock_service_error():
    """Service that yields an error on first call, succeeds on second."""
    call_count = 0
    async def generate_response(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield "[Error: peer closed connection]"
        else:
            yield "Success response after retry"
    
    service = AsyncMock()
    service.generate_response = generate_response
    return service, lambda: call_count


@pytest.fixture
def mock_service_always_error():
    """Service that always yields an error; also exposes a call counter so 'no retry'
    can be asserted by count rather than inferred."""
    call_count = 0
    async def generate_response(**kwargs):
        nonlocal call_count
        call_count += 1
        yield "[Error: provider unavailable]"

    service = AsyncMock()
    service.generate_response = generate_response
    return service, lambda: call_count


def _settings(**overrides):
    """Key-based get_user_setting mock — robust to call order/count (unlike a positional
    side_effect list, which broke whenever a new setting read was added)."""
    base = {
        'autosearch_chat': False,
        'enable_mcp': True,
        'enable_skills': True,
        'auto_retry_on_error': False,
    }
    base.update(overrides)
    return AsyncMock(side_effect=lambda cid, key, default=None: base.get(key, default))


@pytest.fixture
def mock_service_success():
    """Service that always succeeds."""
    async def generate_response(**kwargs):
        yield "Normal successful response"
    
    service = AsyncMock()
    service.generate_response = generate_response
    return service


CHAT_ID = 12345


@pytest.mark.asyncio
async def test_auto_retry_enabled_retries_once(mock_context, mock_service_error):
    """When auto_retry_on_error is enabled and LLM returns error, retry exactly once."""
    service, get_call_count = mock_service_error

    with patch('bot.response_generator.storage_manager') as mock_storage, \
         patch('bot.response_generator._get_provider_configuration') as mock_provider, \
         patch('bot.response_generator.config') as mock_config:

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_user_setting = _settings(auto_retry_on_error=True)
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False
        mock_config.get_chat_max_context_tokens.return_value = 28000

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        # Should have called generate_response twice (original + retry)
        assert get_call_count() == 2
        assert "Success response after retry" in result['content']
        assert result['error'] is None


@pytest.mark.asyncio
async def test_auto_retry_disabled_no_retry(mock_context, mock_service_always_error):
    """When auto_retry_on_error is disabled, do NOT retry."""
    service, get_call_count = mock_service_always_error

    with patch('bot.response_generator.storage_manager') as mock_storage, \
         patch('bot.response_generator._get_provider_configuration') as mock_provider, \
         patch('bot.response_generator.config') as mock_config:

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_user_setting = _settings(auto_retry_on_error=False)
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False
        mock_config.get_chat_max_context_tokens.return_value = 28000

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        assert result['error'] == 'llm_error'
        assert "Error" in result['content']
        # The point of "disabled": the model was invoked exactly once, no retry.
        assert get_call_count() == 1


@pytest.mark.asyncio
async def test_auto_retry_both_fail_returns_error(mock_context, mock_service_always_error):
    """When auto-retry is enabled but both attempts fail, return error (no infinite loop)."""
    service, get_call_count = mock_service_always_error

    with patch('bot.response_generator.storage_manager') as mock_storage, \
         patch('bot.response_generator._get_provider_configuration') as mock_provider, \
         patch('bot.response_generator.config') as mock_config:

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_user_setting = _settings(auto_retry_on_error=True)
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False
        mock_config.get_chat_max_context_tokens.return_value = 28000

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        # Retried exactly once, then returned the error — no infinite loop.
        assert result['error'] == 'llm_error'
        assert "Error" in result['content']
        assert get_call_count() == 2


@pytest.mark.asyncio
async def test_no_retry_on_success(mock_context, mock_service_success):
    """When LLM succeeds, no retry should occur regardless of setting."""
    service = mock_service_success

    with patch('bot.response_generator.storage_manager') as mock_storage, \
         patch('bot.response_generator._get_provider_configuration') as mock_provider, \
         patch('bot.response_generator.config') as mock_config:

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_user_setting = _settings()
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False
        mock_config.get_chat_max_context_tokens.return_value = 28000

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        assert result['error'] is None
        assert "Normal successful response" in result['content']

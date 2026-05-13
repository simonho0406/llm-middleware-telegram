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
    """Service that always yields an error."""
    async def generate_response(**kwargs):
        yield "[Error: provider unavailable]"
    
    service = AsyncMock()
    service.generate_response = generate_response
    return service


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
        # First call returns True (auto_retry setting), subsequent calls return defaults
        mock_storage.get_user_setting = AsyncMock(side_effect=[
            False,  # autosearch_chat (1st call)
            True,   # auto_retry_on_error (1st call - triggers retry)
            False,  # autosearch_chat (retry call)
        ])
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        # Should have called generate_response twice (original + retry)
        assert get_call_count() == 2
        assert "Success response after retry" in result['content']
        assert result['error'] is None


@pytest.mark.asyncio
async def test_auto_retry_disabled_no_retry(mock_context, mock_service_always_error):
    """When auto_retry_on_error is disabled, do NOT retry."""
    service = mock_service_always_error

    with patch('bot.response_generator.storage_manager') as mock_storage, \
         patch('bot.response_generator._get_provider_configuration') as mock_provider, \
         patch('bot.response_generator.config') as mock_config:

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_user_setting = AsyncMock(side_effect=[
            False,  # autosearch_chat
            False,  # auto_retry_on_error (disabled!)
        ])
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        assert result['error'] == 'llm_error'
        assert "Error" in result['content']


@pytest.mark.asyncio
async def test_auto_retry_both_fail_returns_error(mock_context, mock_service_always_error):
    """When auto-retry is enabled but both attempts fail, return error (no infinite loop)."""
    service = mock_service_always_error

    with patch('bot.response_generator.storage_manager') as mock_storage, \
         patch('bot.response_generator._get_provider_configuration') as mock_provider, \
         patch('bot.response_generator.config') as mock_config:

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_user_setting = AsyncMock(side_effect=[
            False,  # autosearch_chat (1st call)
            True,   # auto_retry_on_error (1st call - triggers retry)
            False,  # autosearch_chat (retry call)
            # No more auto_retry_on_error call because is_retry=True skips it
        ])
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        # Should have retried once and then returned the error
        assert result['error'] == 'llm_error'
        assert "Error" in result['content']


@pytest.mark.asyncio
async def test_no_retry_on_success(mock_context, mock_service_success):
    """When LLM succeeds, no retry should occur regardless of setting."""
    service = mock_service_success

    with patch('bot.response_generator.storage_manager') as mock_storage, \
         patch('bot.response_generator._get_provider_configuration') as mock_provider, \
         patch('bot.response_generator.config') as mock_config:

        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_user_setting = AsyncMock(side_effect=[
            False,  # autosearch_chat
            # No auto_retry_on_error call because llm_error_reported_by_model is False
        ])
        mock_storage.save_message = AsyncMock(return_value=1)

        mock_provider.return_value = (
            'nvidia', 'test-model',
            {'enable_streaming': False},
            service,
            {'provider': 'nvidia', 'provider_display': 'NVIDIA', 'model': 'test-model', 'service': service}
        )
        mock_config.get_enable_streaming.return_value = False

        result = await _generate_llm_response(mock_context, CHAT_ID, "test prompt")

        assert result['error'] is None
        assert "Normal successful response" in result['content']

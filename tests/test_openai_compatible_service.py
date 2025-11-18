import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Add project root to path to allow module imports
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.openai_compatible_service import OpenAICompatibleService

@pytest.mark.asyncio
async def test_openai_compatible_service_initialization_and_headers():
    """
    Tests that the OpenAICompatibleService initializes correctly and sets the
    api_version in the headers.
    """
    # 1. Arrange
    mock_provider_config = {
        'name': 'test_provider',
        'base_url': 'https://api.testprovider.com/v1',
        'api_key': 'test_key',
        'api_version': '2024-05-20',
        'default_model': 'test-model',
        'allowed_models': ['test-model']
    }

    # 2. Act
    with patch('services.openai_compatible_service.AsyncOpenAI') as mock_async_openai:
        service_instance = OpenAICompatibleService(mock_provider_config)

        # 3. Assert
        mock_async_openai.assert_called_once()
        kwargs = mock_async_openai.call_args.kwargs
        assert 'default_headers' in kwargs
        headers = kwargs['default_headers']
        assert 'OpenAI-Version' in headers
        assert headers['OpenAI-Version'] == '2024-05-20'

@pytest.mark.asyncio
async def test_generate_response_streaming():
    """
    Tests that generate_response correctly calls the OpenAI client with streaming enabled.
    """
    # 1. Arrange
    mock_provider_config = {
        'name': 'test_provider',
        'base_url': 'https://api.testprovider.com/v1',
        'api_key': 'test_key',
        'default_model': 'test-model',
        'enable_streaming': True
    }
    
    # Mock the AsyncOpenAI client and its methods
    mock_client = MagicMock()
    mock_stream = MagicMock()
    
    # Create an async iterator for the stream
    async def async_iterator(*args, **kwargs):
        yield MagicMock(choices=[MagicMock(delta=MagicMock(content="Hello"))])
        yield MagicMock(choices=[MagicMock(delta=MagicMock(content=" world"))])

    mock_stream.__aiter__ = async_iterator
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch('services.openai_compatible_service.AsyncOpenAI', return_value=mock_client) as mock_async_openai:
        service_instance = OpenAICompatibleService(mock_provider_config)

        # 2. Act
        response_chunks = []
        async for chunk in service_instance.generate_response("test-model", "test prompt"):
            response_chunks.append(chunk)
        
        full_response = "".join(response_chunks)

        # 3. Assert
        mock_client.chat.completions.create.assert_called_once()
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs['stream'] is True
        assert full_response == "Hello world"

@pytest.mark.asyncio
async def test_generate_response_non_streaming():
    """
    Tests that generate_response correctly calls the OpenAI client with streaming disabled.
    """
    # 1. Arrange
    mock_provider_config = {
        'name': 'test_provider',
        'base_url': 'https://api.testprovider.com/v1',
        'api_key': 'test_key',
        'default_model': 'test-model',
        'enable_streaming': False # Explicitly disable streaming
    }
    
    # Mock the AsyncOpenAI client and its methods
    mock_client = MagicMock()
    
    # Mock the non-streaming response
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="This is a non-streaming response."))]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch('services.openai_compatible_service.AsyncOpenAI', return_value=mock_client) as mock_async_openai:
        service_instance = OpenAICompatibleService(mock_provider_config)

        # 2. Act
        response_chunks = []
        async for chunk in service_instance.generate_response("test-model", "test prompt"):
            response_chunks.append(chunk)
        
        full_response = "".join(response_chunks)

        # 3. Assert
        mock_client.chat.completions.create.assert_called_once()
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs['stream'] is False
        assert full_response == "This is a non-streaming response."

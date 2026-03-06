import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock the services modules
sys_mock = MagicMock() # Renamed to avoid conflict with imported sys
ollama_service_mock = MagicMock()
# Make close an async mock
ollama_service_mock.close = AsyncMock()

gemini_service_mock = MagicMock() # No close method

# We need to simulate the bot.providers module state
# Since we can't easily import the actual module with partial mocks without side effects, 
# we will mock the '_initialized_services' dict inside the module after importing it.

from bot import providers

@pytest.mark.asyncio
async def test_shutdown_providers():
    # Setup
    providers._initialized_services = {
        'ollama': ollama_service_mock,
        'gemini': gemini_service_mock
    }
    
    # Execute
    await providers.shutdown_providers()
    
    # Verify
    ollama_service_mock.close.assert_called_once()
    assert len(providers._initialized_services) == 0

@pytest.mark.asyncio
async def test_ollama_singleton_close():
    """Verify that the actual ollama_service module has the close logic."""
    from services import ollama_service
    
    # Reset
    ollama_service._client_instance = AsyncMock()
    mock_client = ollama_service._client_instance
    
    # Mock close/aclose logic on the client
    # Ollama client likely doesn't have .aclose() mocked by AsyncMock by default unless we set it
    # But our code checks hasattr.
    
    # Case 1: Client has aclose (httpx style)
    mock_client._client = AsyncMock()
    mock_client._client.aclose = AsyncMock()
    
    await ollama_service.close()
    
    assert ollama_service._client_instance is None


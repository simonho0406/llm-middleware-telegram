import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from services.openai_compatible_service import OpenAICompatibleService
import config

@pytest.mark.asyncio
async def test_max_retries_is_zero():
    """Verify that AsyncOpenAI client is initialized with max_retries=0."""
    
    mock_config = {
        'name': 'test_provider',
        'base_url': 'http://localhost:1234/v1',
        'api_key': 'fake_key',
        'default_model': 'test_model',
        'max_retries': 3 # Application layer retries
    }
    
    with patch('services.openai_compatible_service.AsyncOpenAI') as MockClient:
        service = OpenAICompatibleService(mock_config)
        
        # Verify AsyncOpenAI was called with max_retries=0
        # The call args are (base_url=..., api_key=..., http_client=..., ..., max_retries=0)
        _, kwargs = MockClient.call_args
        assert kwargs.get('max_retries') == 0, "AsyncOpenAI should be initialized with max_retries=0"

@pytest.mark.asyncio
async def test_close_method_exists_and_works():
    """Verify that the close method exists and calls client.close()."""
    mock_config = {
        'name': 'test_provider',
        'base_url': 'http://localhost:1234/v1',
        'api_key': 'fake_key',
        'default_model': 'test_model'
    }
    
    with patch('services.openai_compatible_service.AsyncOpenAI') as MockClient:
        mock_instance = AsyncMock()
        MockClient.return_value = mock_instance
        
        service = OpenAICompatibleService(mock_config)
        assert hasattr(service, 'close'), "Service should have a close() method"
        
        await service.close()
        mock_instance.close.assert_called_once()

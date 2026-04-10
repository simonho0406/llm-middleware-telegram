import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Add project root to path to allow module imports
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services import gemini_service

@pytest.mark.asyncio
async def test_generate_response_passes_max_output_tokens():
    """
    Tests that generate_response correctly passes the max_output_tokens
    from the config to the Gemini API call using the v2 SDK.
    """
    # 1. Arrange
    # Mock the config functions
    with patch('config.GEMINI_API_KEYS', ['fake_key']):
        with patch('config.get_gemini_max_output_tokens', return_value=4096) as mock_get_tokens:
            # Mock the google.genai Client
            mock_client_instance = MagicMock()
            mock_client_instance.aio.models.generate_content_stream = AsyncMock()
            
            # Create a mock chunk that has a text attribute
            mock_chunk = MagicMock()
            mock_chunk.text = "response"
            mock_chunk.candidates = None # Prevent the finish_reason check from erroring
            
            mock_client_instance.aio.models.generate_content_stream.return_value.__aiter__.return_value = [mock_chunk]

            with patch('google.genai.Client', return_value=mock_client_instance) as mock_gen_client_class:
                
                # 2. Act
                service = gemini_service.GeminiService()
                
                # We only need to consume one item from the generator to trigger the API call
                async for _ in service.generate_response("gemini-pro", "test prompt"):
                    break

                # 3. Assert
                # Assert that our config function was called
                mock_get_tokens.assert_called_once()

                # Assert that the Gemini client was called with the correct generation_config
                mock_client_instance.aio.models.generate_content_stream.assert_called_once()
                
                # Get the keyword arguments passed to the call
                kwargs = mock_client_instance.aio.models.generate_content_stream.call_args.kwargs
                
                assert 'config' in kwargs
                generation_config = kwargs['config']
                # Check that max_output_tokens is set to 4096 in the Types object
                assert generation_config.max_output_tokens == 4096
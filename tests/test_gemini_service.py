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
    from the config to the Gemini API call.
    """
    # 1. Arrange
    # Mock the config functions
    with patch('config.GEMINI_API_KEYS', ['fake_key']):
        with patch('config.get_gemini_max_output_tokens', return_value=4096) as mock_get_tokens:
            # Mock the google.generativeai library
            mock_generative_model = AsyncMock()
            # We need to make the model itself an async iterator to simulate the stream
            mock_generative_model.generate_content_async.return_value.__aiter__.return_value = [MagicMock(text="response")]

            with patch('google.generativeai.GenerativeModel', return_value=mock_generative_model) as mock_gen_model_class:
                
                # 2. Act
                # We only need to consume one item from the generator to trigger the API call
                async for _ in gemini_service.generate_response("gemini-pro", "test prompt"):
                    break

                # 3. Assert
                # Assert that our config function was called
                mock_get_tokens.assert_called_once()

                # Assert that the Gemini client was called with the correct generation_config
                mock_generative_model.generate_content_async.assert_called_once()
                
                # Get the keyword arguments passed to the call
                kwargs = mock_generative_model.generate_content_async.call_args.kwargs
                
                # Check for the presence and value of generation_config
                assert 'generation_config' in kwargs
                generation_config = kwargs['generation_config']
                assert 'max_output_tokens' in generation_config
                assert generation_config['max_output_tokens'] == 4096
import pytest
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from unittest.mock import patch, MagicMock
from utils.context_manager import get_model_context_limits, ModelContextLimits, MODEL_CONTEXT_LIMITS

@pytest.mark.asyncio
async def test_context_limits_respect_config():
    """
    Verify that the context limits are capped by the configuration value.
    """
    # Mock config to return a specific limit (e.g., 100,000)
    with patch('config.get_default_max_context_tokens', return_value=100000):
        
        # Case 1: Model has a larger limit (e.g., Gemini 1.5 Pro with 2M)
        # Should be capped at 100,000
        gemini_model = "gemini-1.5-pro-latest"
        limits = get_model_context_limits(gemini_model, "gemini")
        assert limits.max_context_tokens == 100000
        assert limits.max_context_tokens < MODEL_CONTEXT_LIMITS[gemini_model].max_context_tokens

        # Case 2: Model has a smaller limit (e.g., Llama 3 8B with 8k)
        # Should remain at 8,192 (physical limit)
        llama_model = "meta/llama3-8b-instruct"
        limits = get_model_context_limits(llama_model, "nvidia")
        assert limits.max_context_tokens == 8192
        
        # Case 3: Unknown model (defaults to provider default)
        # Assume provider default is 32k (OpenRouter), config is 100k -> use 32k
        unknown_model = "unknown-model"
        limits = get_model_context_limits(unknown_model, "openrouter")
        assert limits.max_context_tokens == 100000

    # Test with a very low config limit
    with patch('config.get_default_max_context_tokens', return_value=1000):
        gemini_model = "gemini-1.5-pro-latest"
        limits = get_model_context_limits(gemini_model, "gemini")
        assert limits.max_context_tokens == 1000
        # Buffer should be adjusted to not exceed context
        assert limits.buffer_tokens <= 200

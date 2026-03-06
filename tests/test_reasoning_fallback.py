import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.openai_compatible_service import OpenAICompatibleService
from bot.errors import ProviderUnavailableError

class TestReasoningFallback(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_payload_generic_provider(self):
        """Test that generic providers (e.g. Nvidia) get ONLY reasoning_effort to avoid custom schema errors."""
        config = {
            "name": "nvidia",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "test",
            "default_model": "test-model"
        }
        service = OpenAICompatibleService(config)
        
        mock_create = AsyncMock()
        mock_create.return_value.choices = [MagicMock(message=MagicMock(content="Success"))]
        
        with patch.object(service.client.chat.completions, 'create', new=mock_create):
            async for _ in service.generate_response("test-model", "test"): pass
            
        call_kwargs = mock_create.call_args.kwargs
        extra_body = call_kwargs.get('extra_body')
        self.assertIsNotNone(extra_body)
        self.assertNotIn('include_reasoning', extra_body)
        self.assertEqual(extra_body.get('reasoning_effort'), "high")

    async def test_targeted_payload_openrouter(self):
        """Test that OpenRouter gets BOTH reasoning_effort and include_reasoning."""
        config = {
            "name": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test",
            "default_model": "test-model"
        }
        service = OpenAICompatibleService(config)
        
        mock_create = AsyncMock()
        mock_create.return_value.choices = [MagicMock(message=MagicMock(content="Success"))]
        
        with patch.object(service.client.chat.completions, 'create', new=mock_create):
            async for _ in service.generate_response("test-model", "test"): pass
            
        call_kwargs = mock_create.call_args.kwargs
        extra_body = call_kwargs.get('extra_body')
        self.assertIsNotNone(extra_body)
        self.assertEqual(extra_body.get('include_reasoning'), True)
        self.assertEqual(extra_body.get('reasoning_effort'), "high")

    async def test_clean_payload_after_fallback(self):
        """Test that if provider throws 400 (Bad Request), Attempt 2 drops reasoning."""
        from openai import APIStatusError
        import httpx
        
        config = {
            "name": "nvidia",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "test",
            "default_model": "test-model"
        }
        service = OpenAICompatibleService(config)
        
        mock_create = AsyncMock()
        
        # First call fails with 400 (e.g., Nvidia NIM rejecting extra_body)
        # Second call succeeds
        error_response = httpx.Response(400, request=httpx.Request("POST", "url"))
        mock_create.side_effect = [
            APIStatusError("Bad Request", response=error_response, body=None),
            MagicMock(choices=[MagicMock(message=MagicMock(content="Success"))])
        ]
        
        with patch.object(service.client.chat.completions, 'create', new=mock_create):
            async for _ in service.generate_response("test-model", "test"): pass
            
        self.assertEqual(mock_create.call_count, 2)
        
        # Assert first call HAD the reasoning payload
        first_call_kwargs = mock_create.call_args_list[0].kwargs
        self.assertIn('extra_body', first_call_kwargs)
        
        # Assert second call DID NOT HAVE the reasoning payload
        second_call_kwargs = mock_create.call_args_list[1].kwargs
        self.assertNotIn('extra_body', second_call_kwargs)

if __name__ == '__main__':
    unittest.main()

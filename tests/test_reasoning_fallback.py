import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
from services.openai_compatible_service import OpenAICompatibleService
from bot.errors import ProviderUnavailableError

class TestReasoningFallback(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_payload_openrouter(self):
        """Test that openrouter.ai gets include_reasoning."""
        config = {
            "name": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test",
            "default_model": "test-model"
        }
        service = OpenAICompatibleService(config)
        
        mock_create = AsyncMock()
        mock_create.return_value.choices = [MagicMock(message=MagicMock(content="Success"))]
        service.client.chat.completions.create = mock_create
        
        async for _ in service.generate_response("test-model", "test"): pass
            
        call_kwargs = mock_create.call_args.kwargs
        extra_body = call_kwargs.get('extra_body')
        self.assertIsNotNone(extra_body)
        self.assertEqual(extra_body.get('include_reasoning'), True)
        self.assertNotIn('reasoning_effort', extra_body)

    async def test_targeted_payload_openai_o1(self):
        """Test that openai.com o1 models get reasoning_effort."""
        config = {
            "name": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "test",
            "default_model": "o1-preview"
        }
        service = OpenAICompatibleService(config)
        
        mock_create = AsyncMock()
        mock_create.return_value.choices = [MagicMock(message=MagicMock(content="Success"))]
        service.client.chat.completions.create = mock_create
        
        async for _ in service.generate_response("o1-preview", "test"): pass
            
        call_kwargs = mock_create.call_args.kwargs
        extra_body = call_kwargs.get('extra_body')
        self.assertIsNotNone(extra_body)
        self.assertEqual(extra_body.get('reasoning_effort'), "high")

    async def test_clean_payload_generic(self):
        """Test that generic providers (Nvidia/Local) get NO extra params."""
        config = {
            "name": "nvidia",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "test",
            "default_model": "deepseek-ai/deepseek-r1"
        }
        service = OpenAICompatibleService(config)
        
        mock_create = AsyncMock()
        mock_create.return_value.choices = [MagicMock(message=MagicMock(content="Success"))]
        service.client.chat.completions.create = mock_create
        
        async for _ in service.generate_response("deepseek-ai/deepseek-r1", "test"): pass
            
        call_kwargs = mock_create.call_args.kwargs
        self.assertNotIn('extra_body', call_kwargs)

if __name__ == '__main__':
    unittest.main()

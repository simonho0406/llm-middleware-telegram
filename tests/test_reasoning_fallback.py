import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
from services.openai_compatible_service import OpenAICompatibleService
from bot.errors import ProviderUnavailableError

class TestReasoningFallback(unittest.IsolatedAsyncioTestCase):
    async def test_universal_payload_injection(self):
        """Test that generate_response injects the correct reasoning payload."""
        config = {
            "name": "test_provider",
            "base_url": "http://test",
            "api_key": "test",
            "default_model": "test-model"
        }
        service = OpenAICompatibleService(config)
        
        # Mock client create
        mock_create = AsyncMock()
        mock_create.return_value.choices = [MagicMock(message=MagicMock(content="Success"))]
        service.client.chat.completions.create = mock_create
        
        # Run generation
        async for _ in service.generate_response("test-model", "test"):
            pass
            
        # Verify call args
        call_kwargs = mock_create.call_args.kwargs
        extra_body = call_kwargs.get('extra_body')
        self.assertIsNotNone(extra_body)
        self.assertEqual(extra_body['reasoning_effort'], 'high')
        self.assertEqual(extra_body['include_reasoning'], True)
        self.assertEqual(extra_body['thinking'], True)

    async def test_fallback_on_400(self):
        """Test that service retries without reasoning params on 400 error."""
        config = {
            "name": "test_provider",
            "base_url": "http://test",
            "api_key": "test",
            "default_model": "test-model",
            "enable_streaming": False
        }
        service = OpenAICompatibleService(config)
        
        # Mock client create to fail first, then succeed
        from openai import APIStatusError
        
        mock_response_400 = MagicMock()
        mock_response_400.status_code = 400
        error_400 = APIStatusError("Bad Request", response=mock_response_400, body=None)
        
        mock_response_200 = MagicMock()
        success_content = MagicMock()
        success_content.choices = [MagicMock(message=MagicMock(content="Fallback Success"))]
        
        # Side effect: First call raises 400, Second call return success
        service.client.chat.completions.create = AsyncMock(side_effect=[error_400, success_content])
        
        # Run generation
        results = []
        async for chunk in service.generate_response("test-model", "test"):
            results.append(chunk)
            
        # Verify success
        self.assertEqual("".join(results), "Fallback Success")
        
        # Verify TWO calls
        self.assertEqual(service.client.chat.completions.create.call_count, 2)
        
        # Verify first call HAD payload
        args1 = service.client.chat.completions.create.call_args_list[0].kwargs
        self.assertIn('extra_body', args1)
        
        # Verify second call (fallback) did NOT have payload or cleaned it
        args2 = service.client.chat.completions.create.call_args_list[1].kwargs
        self.assertNotIn('extra_body', args2)

if __name__ == '__main__':
    unittest.main()

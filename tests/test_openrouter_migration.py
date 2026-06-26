"""
Tests that the OpenRouter provider is now backed by OpenAICompatibleService
(not the legacy openrouter_service module) and that all required capabilities
(tools forwarding, check_status) are present.
"""
import pytest
import sys
import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.openai_compatible_service import OpenAICompatibleService


# ---------------------------------------------------------------------------
# 1. Provider registry: OpenRouter is an OpenAICompatibleService instance
# ---------------------------------------------------------------------------

def test_openrouter_provider_is_openai_compatible_service():
    """After migration, the openrouter provider in the registry must be an
    OpenAICompatibleService instance — never the legacy module."""
    import config
    if not (config.OPENROUTER_API_KEY and config.OPENROUTER_API_KEY != "YOUR_OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not configured in environment")

    # Reset cache so the test sees a fresh registration
    import bot.providers as providers_mod
    providers_mod._provider_details_cache = None
    providers_mod._initialized_services.pop('openrouter', None)

    details = providers_mod.get_provider_details()
    assert 'openrouter' in details, "openrouter must be registered as a provider"

    svc = details['openrouter']['service']
    assert isinstance(svc, OpenAICompatibleService), (
        f"Expected OpenAICompatibleService, got {type(svc).__name__}"
    )
    assert svc.provider_name == 'openrouter'
    assert 'openrouter.ai' in svc.base_url



# ---------------------------------------------------------------------------
# 2. check_status() exists on OpenAICompatibleService
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_compatible_service_has_check_status():
    """OpenAICompatibleService must have a check_status() coroutine."""
    provider_config = {
        'name': 'openrouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'api_key': 'test-key',
        'default_model': 'test-model',
    }
    svc = OpenAICompatibleService(provider_config)
    assert hasattr(svc, 'check_status'), "check_status method must exist"
    assert callable(svc.check_status)


@pytest.mark.asyncio
async def test_check_status_returns_true_when_models_available():
    provider_config = {
        'name': 'openrouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'api_key': 'test-key',
        'default_model': 'test-model',
        'allowed_models': ['model-a'],
    }
    svc = OpenAICompatibleService(provider_config)

    with patch.object(svc, 'list_models', new_callable=AsyncMock, return_value=['model-a', 'model-b']):
        ok, msg = await svc.check_status()

    assert ok is True
    assert "Online" in msg
    assert "2 models" in msg


@pytest.mark.asyncio
async def test_check_status_returns_false_when_no_models():
    provider_config = {
        'name': 'openrouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'api_key': 'test-key',
        'default_model': 'test-model',
    }
    svc = OpenAICompatibleService(provider_config)

    with patch.object(svc, 'list_models', new_callable=AsyncMock, return_value=[]):
        ok, msg = await svc.check_status()

    assert ok is False
    assert "Offline" in msg or "no models" in msg.lower()


# ---------------------------------------------------------------------------
# 3. tools= forwarding through OpenAICompatibleService (OpenRouter path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_tools_forwarded_to_api():
    """When tools are supplied, OpenAICompatibleService passes them to the API call."""
    provider_config = {
        'name': 'openrouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'api_key': 'test-key',
        'default_model': 'test-model',
        'enable_streaming': False,
    }
    svc = OpenAICompatibleService(provider_config)

    tools = [{
        "type": "function",
        "function": {
            "name": "tavily-search__tavily-search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        }
    }]

    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="result text", tool_calls=None))]
    mock_create = AsyncMock(return_value=mock_response)

    with patch.object(svc.client.chat.completions, 'create', new=mock_create):
        chunks = []
        async for chunk in svc.generate_response("test-model", "find something", tools=tools):
            chunks.append(chunk)

    call_kwargs = mock_create.call_args.kwargs
    assert 'tools' in call_kwargs, "tools must be forwarded to the API call"
    assert call_kwargs['tools'] == tools


@pytest.mark.asyncio
async def test_openrouter_tool_call_response_parsed():
    """When the API responds with tool_calls, they are yielded as a JSON blob."""
    provider_config = {
        'name': 'openrouter',
        'base_url': 'https://openrouter.ai/api/v1',
        'api_key': 'test-key',
        'default_model': 'test-model',
        'enable_streaming': False,
    }
    svc = OpenAICompatibleService(provider_config)

    fake_tool_call = MagicMock()
    fake_tool_call.id = "call_abc"
    fake_tool_call.type = "function"
    fake_tool_call.function.name = "tavily-search__tavily-search"
    fake_tool_call.function.arguments = '{"query": "Rust stable version"}'

    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=None, tool_calls=[fake_tool_call]))]
    mock_create = AsyncMock(return_value=mock_response)

    with patch.object(svc.client.chat.completions, 'create', new=mock_create):
        chunks = []
        async for chunk in svc.generate_response("test-model", "find something"):
            chunks.append(chunk)

    # The last yielded chunk must be a JSON tool_calls blob
    assert len(chunks) > 0
    last_chunk = chunks[-1]
    parsed = json.loads(last_chunk)
    assert "tool_calls" in parsed
    tc = parsed["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "tavily-search__tavily-search"

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Add project root to path to allow module imports
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import httpx
from types import SimpleNamespace
from openai import APIStatusError

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
    
    # Mock the non-streaming response.
    # Explicitly set tool_calls=None so the new tool-call branch doesn't fire
    # (MagicMock auto-creates attributes which are truthy by default).
    mock_message = MagicMock()
    mock_message.content = "This is a non-streaming response."
    mock_message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=mock_message)]
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


# ── Streaming tool-call delta aggregation ────────────────────────────────────────

def _delta_tc(index, id=None, type=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=id, type=type,
                           function=SimpleNamespace(name=name, arguments=arguments))


def _stream_chunk(tool_calls=None, content=None):
    delta = SimpleNamespace(tool_calls=tool_calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


@pytest.mark.asyncio
async def test_streaming_tool_call_delta_aggregation():
    """Tool-call fragments arriving across multiple streaming chunks (id/name in the
    first, argument fragments in later ones) are aggregated by index into a single
    {"tool_calls":[...]} JSON yield with concatenated arguments."""
    cfg = {
        'name': 'test_provider', 'base_url': 'https://api.testprovider.com/v1',
        'api_key': 'test_key', 'default_model': 'test-model', 'enable_streaming': True,
    }
    chunks = [
        _stream_chunk(tool_calls=[_delta_tc(
            0, id='call_1', type='function',
            name='sqlite-tools__read_query', arguments='{"que')]),
        _stream_chunk(tool_calls=[_delta_tc(0, arguments='ry": 1}')]),  # only an arg fragment
    ]

    async def stream_iter(*args, **kwargs):
        for c in chunks:
            yield c

    mock_stream = MagicMock()
    mock_stream.__aiter__ = stream_iter
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

    with patch('services.openai_compatible_service.AsyncOpenAI', return_value=mock_client):
        service = OpenAICompatibleService(cfg)
        out = [chunk async for chunk in service.generate_response("test-model", "prompt")]

    parsed = json.loads(out[-1])
    assert len(parsed["tool_calls"]) == 1
    tc = parsed["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "sqlite-tools__read_query"
    assert tc["function"]["arguments"] == '{"query": 1}'  # fragments concatenated


@pytest.mark.asyncio
async def test_token_overflow_yields_context_too_large_and_stops():
    """A context-overflow APIStatusError yields the 'Context too large' sentinel and
    stops — no retry storm."""
    cfg = {
        'name': 'test_provider', 'base_url': 'https://api.testprovider.com/v1',
        'api_key': 'test_key', 'default_model': 'test-model', 'enable_streaming': False,
    }
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    err = APIStatusError("context length exceeded", response=resp,
                         body={"error": "maximum context length exceeded"})

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=err)

    with patch('services.openai_compatible_service.AsyncOpenAI', return_value=mock_client):
        service = OpenAICompatibleService(cfg)
        out = [chunk async for chunk in service.generate_response("test-model", "prompt")]

    assert any("Context too large" in c for c in out)
    assert mock_client.chat.completions.create.call_count == 1, "must not retry on overflow"


@pytest.mark.asyncio
async def test_role_sanitization_and_none_content_preserved():
    """assistant:panel → assistant; a tool-call assistant turn keeps content=None; a
    tool message gets its name reconstructed from the matching assistant tool_call."""
    cfg = {
        'name': 'test_provider', 'base_url': 'https://api.testprovider.com/v1',
        'api_key': 'test_key', 'default_model': 'test-model', 'enable_streaming': False,
    }
    history = [
        {"role": "assistant:panel", "content": "panel says hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "a", "function": {"name": "srv__t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "result"},
    ]

    mock_message = MagicMock()
    mock_message.content = "ok"
    mock_message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=mock_message)]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch('services.openai_compatible_service.AsyncOpenAI', return_value=mock_client):
        service = OpenAICompatibleService(cfg)
        _ = [chunk async for chunk in service.generate_response("test-model", "prompt", context_history=history)]

    sent = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0]["role"] == "assistant" and sent[0]["content"] == "panel says hi"
    assert sent[1]["role"] == "assistant" and sent[1]["content"] is None and "tool_calls" in sent[1]
    assert sent[2]["role"] == "tool" and sent[2].get("name") == "srv__t"
    assert sent[-1] == {"role": "user", "content": "prompt"}

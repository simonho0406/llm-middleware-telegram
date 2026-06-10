import ollama
import logging
import config
import httpx
from typing import List, Dict, Optional, AsyncGenerator

logger = logging.getLogger(__name__)

_client_instance: Optional[ollama.AsyncClient] = None

def get_ollama_client() -> ollama.AsyncClient:
    """Returns a shared Ollama async client instance with configured timeout."""
    global _client_instance
    if _client_instance is None:
        # Set persistent timeout for the client.
        # This applies to the read timeout (time between chunks), facilitating "slow but steady" generation.
        # We use the config value (default 1200s for Ollama).
        timeout_sec = config.get_ollama_request_timeout_seconds()
        _client_instance = ollama.AsyncClient(host=config.OLLAMA_HOST, timeout=timeout_sec)
    return _client_instance

async def close():
    """Closes the shared Ollama client and releases its httpx connection pool.

    CRITICAL: ollama.AsyncClient wraps an httpx.AsyncClient that captures the
    asyncio event loop at construction time. If we don't aclose() it, the
    next polling-loop iteration (after a NetworkError restart) will reuse
    sockets bound to a closed loop, raising "Event loop is closed" on every
    Ollama call. The leaked sockets also accumulate as FDs over uptime.
    """
    global _client_instance
    if _client_instance is not None:
        # ollama.AsyncClient stores its httpx client as `_client` (private but stable
        # across recent ollama-python versions). Fall back to inspecting attributes
        # so we don't crash if the upstream renames it.
        httpx_client = getattr(_client_instance, '_client', None)
        if httpx_client is not None:
            try:
                await httpx_client.aclose()
            except Exception as e:
                logger.warning(f"Non-fatal error closing Ollama httpx client: {e}")
        _client_instance = None
        logger.info("Ollama client closed and connection pool released.")

async def check_ollama_health(client: ollama.AsyncClient) -> bool:
    """
    Performs a quick and reliable health check of the Ollama server.
    """
    try:
        # Use a short timeout for health checks
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            response = await http_client.get(config.OLLAMA_HOST)
            return response.status_code == 200 and "Ollama is running" in response.text
    except httpx.RequestError as e:
        logger.error(f"Ollama health check failed: {e}")
        return False

async def is_model_available(client: ollama.AsyncClient, model_name: str) -> bool:
    """
    Checks if a specific model is available on the Ollama server.
    """
    try:
        response_dict = await client.list()
        models_list = response_dict.get('models', [])
        for model_details in models_list:
            name = model_details.get('name') or model_details.get('model')
            if name == model_name:
                return True
        return False
    except Exception as e:
        logger.exception(f"Error checking model availability: {e}")
        return False

async def list_models() -> List[str]:
    """Fetches the list of available models from Ollama."""
    client = get_ollama_client()
    if not await check_ollama_health(client):
        logger.warning("Ollama server is not reachable. Cannot list models.")
        return []
    try:
        response_dict = await client.list()
        model_names = []
        models_list = response_dict.get('models', [])
        for model_details in models_list:
            name = model_details.get('name') or model_details.get('model')
            if name:
                model_names.append(name)
            else:
                logger.warning(f"Could not extract model name from Ollama list response item: {model_details}")
        logger.info(f"Available Ollama models: {model_names}")
        return model_names
    except Exception as e:
        logger.exception(f"Error fetching Ollama models: {e}")
        return []

async def _generate_single_model_non_streaming(model: str, prompt: str, context_history: Optional[List[Dict]] = None) -> str:
    """Generates a non-streaming response from Ollama for concurrent queries."""
    client = get_ollama_client()
    if not await check_ollama_health(client):
        return "[Error: Ollama server not available]"
    if not await is_model_available(client, model):
        return f"[Error: Model '{model}' is not available on the Ollama server.]"

    messages = []
    if context_history:
        for msg in context_history:
            role = msg.get('role', 'user').lower()
            
            # Map internal roles
            if role == 'assistant:panel':
                role = 'assistant'
            
            if role not in ['user', 'assistant']: 
                role = 'user'
            messages.append({'role': role, 'content': msg.get('content', '')})
    messages.append({'role': 'user', 'content': prompt})

    logger.info(f"Sending non-streaming request to Ollama model '{model}'")
    try:
        response = await client.chat(
            model=model,
            messages=messages,
            stream=False
        )
        return response['message']['content'].strip()
    except ollama.ResponseError as e:
        logger.error(f"Ollama API Error (model: {model}): {e.error}")
        return f"[Error: {e.error}]"
    except Exception as e:
        logger.exception(f"Error generating Ollama response (model: {model}): {e}")
        return f"[Error: {str(e)}]"

async def generate_response(model: str, prompt: str, context_history: Optional[List[Dict]] = None, request_timeout: int = None) -> AsyncGenerator[str, None]:
    """
    Generates a response from the specified Ollama model using streaming.
    """
    client = get_ollama_client()
    if not await check_ollama_health(client):
        logger.warning("Ollama server is not reachable. Cannot generate response.")
        yield "[Error: Ollama server not available]"
        return
    if not await is_model_available(client, model):
        logger.warning(f"Model '{model}' is not available on the Ollama server.")
        yield f"[Error: Model '{model}' is not available on the Ollama server.]"
        return

    messages = []
    if context_history:
        messages.extend(context_history)
    if prompt:
        messages.append({'role': 'user', 'content': prompt})

    logger.info(f"Sending request to Ollama model '{model}'")
    try:
        # Note: request_timeout in 'options' is often ignored by Ollama unless implemented by the model runner.
        # We rely on the client.timeout set in get_ollama_client() for network resiliency.
        options = {}
        
        async for part in await client.chat(model=model, messages=messages, stream=True, options=options):
            if hasattr(part, 'message') and hasattr(part.message, 'content'):
                chunk = part.message.content
                yield chunk
            if hasattr(part, 'done') and part.done:
                # Use model_dump() for pydantic objects to get a dictionary
                final_metrics = part.model_dump(exclude={'message', 'done'})
                if final_metrics:
                    logger.info(f"Ollama generation finished successfully. Metrics: {final_metrics}")
                break
    except ollama.ResponseError as e:
        logger.error(f"Ollama API Response Error (model: {model}): {e.error} (Status: {e.status_code})")
        yield f"[Error: Ollama API error - {e.error}]"
    except Exception as e:
        logger.exception(f"Unexpected error during Ollama stream generation (model: {model}): {e}")
        yield f"[Error: Unexpected error during Ollama generation - {str(e)}]"

async def check_status() -> (bool, str):
    """Checks if the Ollama server is reachable."""
    client = get_ollama_client()
    is_healthy = await check_ollama_health(client)
    message = f"Service is {'reachable' if is_healthy else 'unreachable'} at {config.OLLAMA_HOST}"
    return is_healthy, message

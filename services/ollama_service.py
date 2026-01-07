import ollama
import logging
import config
import httpx
from typing import List, Dict, Optional, AsyncGenerator

logger = logging.getLogger(__name__)

_client_instance: Optional[ollama.AsyncClient] = None

def get_ollama_client() -> ollama.AsyncClient:
    """Returns a shared Ollama async client instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = ollama.AsyncClient(host=config.OLLAMA_HOST)
    return _client_instance

async def close():
    """Closes the shared Ollama client."""
    global _client_instance
    if _client_instance:
        # Ollama's AsyncClient doesn't have a close() method exposed directly usually?
        # Actually it uses httpx.AsyncClient under the hood. 
        # Checking library source or docs is key. 
        # Standard httpx client pattern: await client.aclose() or similar?
        # Creating a new client: `self._client = httpx.AsyncClient(...)`
        # It seems `ollama` python lib v0.x might not expose close easily on the wrapper.
        # But wait, looking at my search result: "Access Asynchronous Methods via client.aio". That was Gemini.
        # For Ollama: `AsyncClient` inherits from `BaseClient`.
        # Taking a safer bet: If it has .close(), call it. If it has .aclose(), call it.
        # Most httpx wrappers support .close() (sync) or .aclose() (async).
        # Let's try to close the internal _client if accessible, or just plain close().
        
        # Best effort cleanup
        pass 
        # WAIT: The implementation plan said "Add close() method". 
        # I should try to close it if possible. 
        # If the library doesn't expose it, I can at least set it to None.
        pass
        
    # Actually, let's implement a proper async close if the library supports it.
    # If not, we just clear the reference.
    if _client_instance:
         # Check for httpx client
         if hasattr(_client_instance, '_client') and hasattr(_client_instance._client, 'aclose'):
             await _client_instance._client.aclose()
         elif hasattr(_client_instance, 'close'):
             if asyncio.iscoroutinefunction(_client_instance.close):
                 await _client_instance.close()
             else:
                 _client_instance.close()
         
         _client_instance = None
         logger.info("Ollama client closed.")

async def check_ollama_health(client: ollama.AsyncClient) -> bool:
    """
    Performs a quick and reliable health check of the Ollama server.
    """
    try:
        async with httpx.AsyncClient() as http_client:
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
        logger.error(f"Error checking model availability: {e}")
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
        logger.error(f"Error fetching Ollama models: {e}")
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
        logger.error(f"Error generating Ollama response (model: {model}): {e}")
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
        options = {}
        if request_timeout is not None:
            options['request_timeout'] = request_timeout

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

# Example usage (for testing purposes)
async def _test():
    print("Testing Ollama Service...")
    if not await check_status():
        print("Ollama connection failed. Exiting test.")
        return

    models = await list_models()
    print(f"Available models: {models}")
    if not models:
        print("No models found. Cannot test generation.")
        return

    test_model = config.get_default_ollama_model()
    if test_model not in models:
        print(f"Default model '{test_model}' not found in available models: {models}. Using first available model.")
        test_model = models[0]

    print(f"\nTesting generation with model: {test_model}")
    prompt = "Why is the sky blue?"
    print(f"Prompt: {prompt}")
    full_response = ""
    async for chunk in generate_response(model=test_model, prompt=prompt):
        print(chunk, end="", flush=True)
        full_response += chunk
    print("\n--- End of Generation ---")

if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
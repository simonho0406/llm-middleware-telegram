import ollama
import logging
import config
from typing import List, Dict, Optional, AsyncGenerator

logger = logging.getLogger(__name__)

# Initialize the Ollama client
# Consider error handling if the host is unreachable during initialization
try:
    client = ollama.AsyncClient(host=config.OLLAMA_HOST)
    logger.info(f"Ollama client initialized for host: {config.OLLAMA_HOST}")
except Exception as e:
    logger.error(f"Failed to initialize Ollama client: {e}")
    client = None # Indicate failure

async def list_models() -> List[str]:
    """Fetches the list of available models from Ollama."""
    if not client:
        logger.warning("Ollama client not initialized. Cannot list models.")
        return []
    try:
        # The client.list() returns a dictionary, the models are under the 'models' key
        response_dict = await client.list()
        model_names = []
        # The value associated with 'models' key is a list of model details objects/dicts
        models_list = response_dict.get('models', [])
        for model_details in models_list:
             # The ollama library might return dicts or objects depending on version/context
             # Let's try accessing 'name' first, then 'model' as seen in logs
             name = None
             if isinstance(model_details, dict):
                 name = model_details.get('name') or model_details.get('model')
             elif hasattr(model_details, 'name'):
                 name = model_details.name
             elif hasattr(model_details, 'model'):
                 name = model_details.model # Based on log output: model='qwq:latest'

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
    if not client:
        return "[Error: Ollama client not available]"

    messages = []
    if context_history:
        for msg in context_history:
            role = msg.get('role', 'user').lower()
            if role not in ['user', 'assistant']: 
                role = 'user'
            messages.append({'role': role, 'content': msg.get('content', '')})
    messages.append({'role': 'user', 'content': prompt})

    logger.info(f"Sending non-streaming request to Ollama model '{model}'")
    try:
        response = await client.chat(
            model=model,
            messages=messages,
            stream=False # Non-streaming request
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

    Args:
        model: The name of the Ollama model to use.
        prompt: The user's prompt.
        context_history: Optional list of previous messages for context
                         (e.g., [{'role': 'user', 'content': '...'}, {'role': 'assistant', 'content': '...'}])

    Yields:
        Chunks of the generated response string.
    """
    if not client:
        logger.warning("Ollama client not initialized. Cannot generate response.")
        yield "[Error: Ollama client not available]"
        return # Use return instead of raise StopAsyncIteration in generator

    messages = []
    if context_history:
        messages.extend(context_history)
    messages.append({'role': 'user', 'content': prompt})

    logger.info(f"Sending request to Ollama model '{model}'")
    try:
        # Use stream=True for asynchronous streaming
        options = {}
        if request_timeout is not None:
            options['request_timeout'] = request_timeout

        async for part in await client.chat(model=model, messages=messages, stream=True, options=options):
            if part.message and part.message.content:
                chunk = part.message.content
                yield chunk
            if part.done:  # Check using Pydantic model attribute
                final_metrics = part.model_dump(exclude={'message', 'done'})
                if final_metrics:
                    logger.info(f"Ollama generation finished successfully. Metrics: {final_metrics}")
                # Successfully finished, do not yield any error here.
                # The loop finishes naturally.
                break # Exit the loop once done
        # If the loop completes without exceptions, we are done.
    except ollama.ResponseError as e:
        # This handles specific API errors returned by Ollama during the stream
        logger.error(f"Ollama API Response Error (model: {model}): {e.error} (Status: {e.status_code})")
        yield f"[Error: Ollama API error - {e.error}]"
    except Exception as e:
        # This handles other unexpected errors DURING the streaming attempt
        logger.exception(f"Unexpected error during Ollama stream generation (model: {model}): {e}")
        yield f"[Error: Unexpected error during Ollama generation - {str(e)}]"
    # DO NOT yield a generic error message outside the try/except block
    # If the stream finishes successfully, the generator should just stop.

async def check_connection() -> bool:
    """Checks if the Ollama server is reachable."""
    if not client:
        return False
    try:
        # The list() method is a good way to check basic connectivity
        await client.list()
        logger.info("Ollama connection successful.")
        return True
    except Exception as e:
        logger.error(f"Ollama connection failed: {e}")
        return False

# Example usage (for testing purposes)
async def _test():
    print("Testing Ollama Service...")
    if not await check_connection():
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

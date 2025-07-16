import httpx
import logging
import config
import asyncio
import json
from typing import AsyncGenerator, List, Dict, Optional, Any # Import Any

logger = logging.getLogger(__name__)

async def generate_response(
    model: str,
    prompt: str,
    context_history: Optional[List[Dict]] = None, # Use Optional and correct type hint
    request_timeout: int = None
) -> AsyncGenerator[str, None]: # Correct return type hint for async generator
    """
    Sends a request to OpenRouter API with streaming support.
    Handles rate limits and errors gracefully.
    """
    if not config.OPENROUTER_API_KEY or config.OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        logger.warning("OpenRouter API Key not configured.")
        yield "[Error: OpenRouter API Key not configured]"
        return

    headers = {
        "HTTP-Referer": config.OPENROUTER_HTTP_REFERER,  # Use configured HTTP Referer
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    # Prepare messages for OpenRouter (similar to OpenAI format)
    messages = []
    if context_history:
        for msg in context_history:
             # Ensure roles are 'user' or 'assistant'
             role = msg.get('role', 'user').lower()
             if role not in ['user', 'assistant']:
                 role = 'user' # Default to user if role is invalid
             messages.append({"role": role, "content": msg.get('content', '')})
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": model,
        "messages": messages, # Use messages format
        "temperature": 0.7,
        "stream": True
    }

    try:
        if isinstance(data, dict):
            logger.debug(f"Sending request to OpenRouter. Model: {model}. Payload: {json.dumps(data, indent=2)}")
        else:
            logger.error(f"OpenRouter payload is not a dictionary. Type: {type(data)}. Value: {data}")
    except NameError:
        logger.error("json module not found when attempting to log OpenRouter payload.")
    except Exception as e:
        logger.error(f"Error dumping OpenRouter payload for logging: {e}. Payload: {data}")

    retries = 3
    delay = 1.0
    for attempt in range(retries):
        try:
            timeout_config = request_timeout if request_timeout is not None else 30.0
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                async with client.stream(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=data
                ) as response:
                    response.raise_for_status() # Raise an exception for 4xx/5xx errors
                    async for line in response.aiter_lines():
                        # ... (existing stream processing logic) ...
                        if line.startswith("data: "):
                            line_data = line[len("data: "):].strip()
                            if line_data == "[DONE]":
                                break
                            try:
                                chunk_data = json.loads(line_data)
                                if 'choices' in chunk_data and chunk_data['choices']:
                                    delta = chunk_data['choices'][0].get('delta', {})
                                    content = delta.get('content')
                                    if content:
                                        yield content
                            except json.JSONDecodeError:
                                logger.warning(f"Received non-JSON data line: {line_data}")
                    return # Success, exit the retry loop

        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.HTTPStatusError) as e:
            logger.warning(f"OpenRouter request failed (Attempt {attempt + 1}/{retries}): {e}")
            if attempt == retries - 1:
                logger.error("OpenRouter request failed after all retries.")
                yield f"[Error: The request to OpenRouter failed after {retries} attempts. Details: {e}]"
                return
            await asyncio.sleep(delay)
            delay *= 2
        except Exception as e:
            logger.exception("Unexpected error in OpenRouter service")
            yield f"[Error: Unexpected error - {str(e)}]"
            return


async def check_connection() -> bool:
    """Checks if the OpenRouter API key is valid by fetching available models."""
    if not config.OPENROUTER_API_KEY or config.OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        logger.warning("OpenRouter API Key not configured, skipping connection check.")
        return False # Cannot check without a key

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
            response.raise_for_status() # Raises exception for 4xx/5xx errors
            logger.info("OpenRouter connection check successful (fetched models).")
            return True
    except httpx.HTTPStatusError as http_err:
        logger.error(f"OpenRouter connection check failed: HTTP Error {http_err.response.status_code}")
        return False
    except httpx.RequestError as req_err:
        logger.error(f"OpenRouter connection check failed: Request Error {req_err}")
        return False
    except Exception as e:
        logger.exception("Unexpected error during OpenRouter connection check")
        return False

async def list_models() -> List[Dict[str, Any]]:
    """Fetches the list of models from OpenRouter and filters for free ones."""
    if not config.OPENROUTER_API_KEY or config.OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        logger.warning("OpenRouter API Key not configured, cannot fetch models.")
        return []

    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}
    free_models = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
            response.raise_for_status()
            models_data = response.json().get("data", [])

            for model in models_data:
                # Check pricing: prompt price is 0 and completion price is 0
                pricing = model.get("pricing", {})
                prompt_cost = float(pricing.get("prompt", "1")) # Default to non-zero if missing
                completion_cost = float(pricing.get("completion", "1")) # Default to non-zero if missing

                if prompt_cost == 0.0 and completion_cost == 0.0:
                    free_models.append({
                        "id": model.get("id"),
                        "name": model.get("name", model.get("id")), # Use name if available
                        # Add other relevant info if needed, e.g., context_length
                        "context_length": model.get("context_length")
                    })
            logger.info(f"Fetched {len(free_models)} free models from OpenRouter.")

    except httpx.HTTPStatusError as http_err:
        logger.error(f"Failed to fetch OpenRouter models: HTTP Error {http_err.response.status_code}")
    except httpx.RequestError as req_err:
        logger.error(f"Failed to fetch OpenRouter models: Request Error {req_err}")
    except Exception as e:
        logger.exception("Unexpected error fetching OpenRouter models")

    return free_models

async def _generate_single_model_non_streaming(model_id: str, prompt: str, context_history: Optional[List[Dict]]) -> str:
    """Helper function to generate response from a single model (non-streaming)."""
    if not config.OPENROUTER_API_KEY or config.OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        return "[Error: OpenRouter API Key not configured]"

    headers = {
        "HTTP-Referer": config.OPENROUTER_HTTP_REFERER, # Use configured HTTP Referer
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    messages = []
    if context_history:
        for msg in context_history:
             role = msg.get('role', 'user').lower()
             if role not in ['user', 'assistant']: role = 'user'
             messages.append({"role": role, "content": msg.get('content', '')})
    messages.append({"role": "user", "content": prompt})

    data = { "model": model_id, "messages": messages, "temperature": 0.7, "stream": False } # stream=False

    try:
        # Use a longer timeout for potentially slower non-streaming models
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=data
            )
            response.raise_for_status() # Check for HTTP errors
            response_data = response.json()

            if 'choices' in response_data and response_data['choices']:
                content = response_data['choices'][0].get('message', {}).get('content')
                return content.strip() if content else "[Empty Response]"
            else:
                logger.warning(f"Unexpected response structure from OpenRouter model {model_id}: {response_data}")
                return "[Error: Unexpected response structure]"

    except httpx.HTTPStatusError as http_err:
        logger.error(f"HTTP Error during concurrent OpenRouter generation (Model: {model_id}): {http_err}")
        try:
            err_details = http_err.response.json().get('error', {}).get('message', str(http_err))
        except: # Handle cases where error response is not JSON
            err_details = str(http_err)
        return f"[HTTP Error {http_err.response.status_code}: {err_details}]"
    except httpx.ReadTimeout:
        logger.error(f"Timeout during concurrent OpenRouter generation (Model: {model_id})")
        return "[Error: Request timed out]"
    except Exception as e:
        logger.exception(f"Unexpected error during concurrent OpenRouter generation (Model: {model_id})")
        return f"[Error: {str(e)}]"


async def generate_concurrent_free_responses(prompt: str, context_history: Optional[List[Dict]] = None) -> Dict[str, str]:
    """
    Generates responses concurrently from all available FREE OpenRouter models.

    Args:
        prompt: The user's prompt.
        context_history: Optional list of previous messages for context.

    Returns:
        A dictionary mapping model IDs to their responses (or error messages).
    """
    free_models = await list_models()
    if not free_models:
        logger.warning("No free OpenRouter models found or could not fetch list.")
        return {"error": "[Error: Could not fetch or find any free OpenRouter models]"}

    model_ids = [m['id'] for m in free_models]
    logger.info(f"Sending concurrent requests to {len(model_ids)} free OpenRouter models: {model_ids}")

    tasks = []
    for model_id in model_ids:
        tasks.append(
            _generate_single_model_non_streaming(model_id, prompt, context_history)
        )

    # Run tasks concurrently and gather results
    # Use asyncio.gather with return_exceptions=True
    import asyncio
    results = await asyncio.gather(*tasks, return_exceptions=True)

    response_dict = {}
    for model_id, result in zip(model_ids, results):
        if isinstance(result, Exception):
            logger.error(f"Unhandled exception during concurrent generation for model {model_id}: {result}")
            response_dict[model_id] = f"[Unhandled Exception: {result}]"
        else:
            response_dict[model_id] = result # result is already the string response or error string

    return response_dict

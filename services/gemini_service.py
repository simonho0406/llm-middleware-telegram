import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
import logging
import asyncio
from itertools import cycle
import config
from typing import List, Dict, Optional, AsyncGenerator, Any # Import Any

logger = logging.getLogger(__name__)

# --- Key Management ---
_gemini_keys = config.GEMINI_API_KEYS
_key_iterator = cycle(_gemini_keys) if _gemini_keys else None
_current_key_index = 0

def _get_next_key():
    """Gets the next key in a round-robin fashion."""
    global _current_key_index
    if not _gemini_keys:
        return None
    key = next(_key_iterator)
    try:
        _current_key_index = _gemini_keys.index(key)
    except ValueError:
        _current_key_index = -1
    logger.debug(f"Using Gemini API Key index: {_current_key_index}")
    return key

# --- Service Functions ---

async def _generate_single_model_non_streaming(model: str, prompt: str, context_history: Optional[List[Dict]] = None) -> str:
    """Generates a non-streaming response from Gemini for concurrent queries."""
    if not _gemini_keys:
        return "[Error: Gemini API keys not configured]"

    gemini_history = []
    if context_history:
        for msg in context_history:
            role = 'user' if msg.get('role') == 'user' else 'model'
            content = msg.get('content', '')
            gemini_history.append({'role': role, 'parts': [content]})

    try:
        current_key = _get_next_key()
        genai.configure(api_key=current_key)
        gemini_model = genai.GenerativeModel(model)
        full_prompt = gemini_history + [{'role': 'user', 'parts': [prompt]}]

        # Run synchronous call in a thread with timeout
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_model.generate_content,
                    contents=full_prompt,
                    stream=False
                ),
                timeout=config.REQUEST_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(f"Gemini non-streaming call timed out after {config.REQUEST_TIMEOUT_SECONDS}s (Key {_current_key_index}, Model: {model})")
            return "[Error: Request timed out]"


        if response.prompt_feedback.block_reason:
            return f"[Blocked: {response.prompt_feedback.block_reason}]"

        return response.text.strip() if hasattr(response, 'text') else "[Empty Response]"

    except google_exceptions.ResourceExhausted as e:
        logger.warning(f"Gemini rate limit (Key {_current_key_index}): {e}")
        return f"[Rate Limit: {e}]"
    except Exception as e:
        logger.error(f"Gemini error (Key {_current_key_index}, Model: {model}): {e}")
        return f"[Error: {e}]"

async def generate_response(model: str, prompt: str, context_history: Optional[List[Dict]] = None) -> AsyncGenerator[str, None]:
    """
    Generates a response from the specified Gemini model using streaming with key rotation.
    """
    if not _gemini_keys:
        logger.warning("No Gemini API keys configured.")
        yield "[Error: Gemini API keys not configured]"
        return

    gemini_history = []
    if context_history:
        for msg in context_history:
            role = 'user' if msg.get('role') == 'user' else 'model'
            content = msg.get('content', '')
            gemini_history.append({'role': role, 'parts': [content]})

    logger.info(f"Sending request to Gemini model '{model}'")
    full_prompt = gemini_history + [{'role': 'user', 'parts': [prompt]}]

    initial_key_index = _current_key_index
    attempts = 0
    max_attempts = len(_gemini_keys)

    while attempts < max_attempts:
        current_key = _get_next_key()
        if not current_key:
             yield "[Error: No Gemini keys available]"
             return

        try:
            genai.configure(api_key=current_key)
            logger.debug(f"Attempting Gemini stream with Key Index: {_current_key_index}")
            gemini_model = genai.GenerativeModel(model)

            # Run the synchronous stream generation and iteration in a separate thread
            processed_chunks = 0 # Define here to be accessible in stream_processor via nonlocal if needed, or just pass/return
            async def stream_processor():
                # nonlocal processed_chunks # Allow modification if needed, though direct yield is better
                _processed_count = 0 # Local counter for this attempt
                try:
                    # Get the synchronous iterator in the thread
                    response_stream = await asyncio.to_thread(
                        gemini_model.generate_content,
                        contents=full_prompt,
                        stream=True
                    )
                    # Iterate over the synchronous iterator within the same thread
                    for chunk in response_stream:
                        _processed_count += 1

                        # --- Check for blocking reason FIRST ---
                        block_reason = chunk.prompt_feedback.block_reason
                        if block_reason:
                            logger.warning(f"Gemini content blocked (Key Index: {_current_key_index}, Reason: {block_reason})")
                            yield f"[Error: Content blocked by Gemini - {block_reason}]"
                            return # Indicate processing stopped due to blocking

                    # Yield the text content if available
                        if hasattr(chunk, 'parts') and chunk.parts:
                            for part in chunk.parts:
                                if hasattr(part, 'text'):
                                    yield part.text
                        # else: logger.debug(f"Received non-text chunk: {chunk}")

                    # Stream finished successfully
                    logger.debug(f"Gemini stream finished after {_processed_count} chunks (Key Index: {_current_key_index}).")
                    if _processed_count == 0:
                        logger.warning(f"Gemini stream finished without yielding any text chunks (Key Index: {_current_key_index}, possibly due to safety filters).")
                        yield "[Error: Gemini returned no content. This might be due to safety filters or the specific prompt.]"
                    # If we reach here, the stream finished normally within this attempt.
                    # No need to return a value from the async generator.
                    return

                except google_exceptions.ResourceExhausted as thread_e:
                    # Re-raise to be caught by the outer loop for key rotation
                    logger.warning(f"Gemini Rate Limit Error during stream iteration (Key Index: {_current_key_index}): {thread_e}")
                    raise thread_e
                except Exception as thread_e:
                    # Catch other errors during iteration
                    logger.error(f"Error processing Gemini stream chunk (Key Index: {_current_key_index}): {thread_e}")
                    yield f"[Error processing stream: {thread_e}]"
                    return # Indicate processing stopped due to error

            # Execute the stream processor and yield its chunks/errors.
            try:
                # Iterate over the async generator returned by stream_processor
                async for chunk_or_error in stream_processor():
                    yield chunk_or_error # Yield the chunk/error message back to the caller of generate_response

                # If the async for loop completes without exceptions that need key rotation,
                # it means the stream finished successfully for this key.
                return # Exit the generate_response function successfully.

            except google_exceptions.ResourceExhausted as e:
                # This exception is raised by stream_processor if it occurs during iteration
                logger.warning(f"Gemini Rate Limit Error during stream (Key Index: {_current_key_index}): {e}. Trying next key.")
                # Let the main loop handle attempts increment and retry logic
                attempts += 1
                if attempts >= max_attempts:
                    logger.error(f"All Gemini keys hit rate limits after stream error. Last error: {e}")
                    yield f"[Error: Gemini rate limit reached on all keys - {e}]"
                    return
                # Continue to the next key attempt in the while loop (handled by the loop structure)

            # Note: TimeoutError from wait_for inside stream_processor might need specific handling
            # if it's re-introduced there. Currently, no top-level timeout on the async for.

            except Exception as e:
                 # Catch other potential errors raised during stream_processor iteration
                 logger.error(f"Error during Gemini stream processing (Key Index: {_current_key_index}, Model: {model}): {e}")
                 # Decide if this error warrants trying the next key or stopping.
                 # For now, assume it might be key-specific and try the next one.
                 attempts += 1
                 if attempts >= max_attempts:
                     logger.error(f"All Gemini keys failed during stream processing. Last error: {e}")
                     yield f"[Error: Could not process stream from Gemini after trying all keys - {e}]"
                     return
                 # Continue to the next key attempt

        # This outer try/except block catches errors *before* stream_processor is called
        # or exceptions re-raised *from* stream_processor that indicate a need for key rotation.
        except google_exceptions.ResourceExhausted as e:
            logger.warning(f"Gemini Rate Limit Error (Initial Setup or Re-raised) (Key Index: {_current_key_index}): {e}. Trying next key.")
            attempts += 1
            if attempts >= max_attempts:
                 logger.error(f"All Gemini keys hit rate limits. Last error: {e}")
                 yield f"[Error: Gemini rate limit reached on all keys - {e}]"
                 return
            # Continue loop to try next key
        except Exception as e:
            # Catch other potential errors like invalid API key, model not found, etc., during setup.
            logger.error(f"Error setting up Gemini request (Key Index: {_current_key_index}, Model: {model}): {e}")
            attempts += 1
            if attempts >= max_attempts:
                 logger.error(f"All Gemini keys failed during setup. Last error: {e}")
                 yield f"[Error: Could not initiate request to Gemini after trying all keys - {e}]"
                 return
            # Continue loop to try next key

    # This part is reached only if the while loop completes without returning (e.g., max_attempts reached without success)
    logger.error("Gemini generation loop completed without successfully returning or yielding a final error for all keys.")
    yield "[Error: Unexpected exit from Gemini generation loop after trying all keys]"


async def check_connection() -> bool:
    """Checks if *any* configured Gemini API key is valid."""
    if not _gemini_keys:
        return False

    initial_key_index = _current_key_index
    success = False
    for _ in range(len(_gemini_keys)):
        key_to_check = _get_next_key()
        if not key_to_check: continue

        try:
            genai.configure(api_key=key_to_check)
            await asyncio.to_thread(genai.list_models)
            logger.info(f"Gemini connection successful (checked with Key Index: {_current_key_index}).")
            success = True
            break
        except Exception as e:
            logger.warning(f"Gemini connection check failed for Key Index {_current_key_index}: {e}")

    # Optionally reset iterator to initial state if needed
    # global _key_iterator, _current_key_index
    # _key_iterator = cycle(_gemini_keys)
    # _current_key_index = 0
    # for _ in range(initial_key_index): next(_key_iterator)

    if not success:
        logger.error("Gemini connection check failed for all provided keys.")
    return success

async def list_models() -> List[Dict[str, Any]]:
    """Lists available Gemini models suitable for generateContent."""
    if not _gemini_keys:
        logger.warning("Cannot list Gemini models: No API keys configured.")
        return []

    key_to_use = _get_next_key() # Use the next key for listing
    if not key_to_use:
        logger.error("Cannot list Gemini models: Failed to get an API key.")
        return []

    try:
        genai.configure(api_key=key_to_use)
        logger.debug(f"Listing Gemini models using Key Index: {_current_key_index}")
        
        all_models_iter = await asyncio.to_thread(genai.list_models)
        
        generative_models = []
        for m in all_models_iter:
            # Filter for models that support 'generateContent'
            if 'generateContent' in m.supported_generation_methods:
                # Extract the base model name (e.g., 'gemini-1.5-pro-latest')
                # The API returns names like 'models/gemini-1.5-pro-latest'
                base_model_name = m.name.split('/')[-1]
                generative_models.append({"id": base_model_name, "name": m.display_name})
                
        logger.info(f"Found {len(generative_models)} Gemini models supporting generateContent.")
        return generative_models
    except Exception as e:
        logger.error(f"Failed to list Gemini models (Key Index: {_current_key_index}): {e}")
        return [] # Return empty list on error

# Note: _generate_single_model_response and generate_concurrent_responses remain largely the same
# as they correctly use asyncio.to_thread for non-streaming calls.

async def _generate_single_model_response(model: str, prompt: str, context_history: Optional[List[Dict]], key: str) -> str:
    """Helper function to generate response from a single model using a specific key."""
    try:
        genai.configure(api_key=key)
        gemini_model = genai.GenerativeModel(model)
        full_prompt = (context_history or []) + [{'role': 'user', 'parts': [prompt]}]

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_model.generate_content,
                    contents=full_prompt,
                    stream=False
                ),
                timeout=config.REQUEST_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(f"Gemini concurrent call timed out after {config.REQUEST_TIMEOUT_SECONDS}s (Model: {model})")
            return "[Error: Request timed out]"


        if response.prompt_feedback.block_reason:
            return f"[Blocked by Gemini: {response.prompt_feedback.block_reason}]"

        return response.text.strip() if hasattr(response, 'text') else "[Empty Response]"

    except google_exceptions.ResourceExhausted as e:
        logger.warning(f"Gemini Rate Limit Error during concurrent generation (Model: {model}): {e}")
        return f"[Rate Limit Error: {e}]"
    except Exception as e:
        logger.error(f"Error during concurrent Gemini generation (Model: {model}): {e}")
        return f"[Error: {e}]"


async def generate_concurrent_responses(prompt: str, context_history: Optional[List[Dict]] = None) -> Dict[str, str]:
    """
    Generates responses from multiple configured Gemini models concurrently.
    """
    if not _gemini_keys:
        logger.warning("No Gemini API keys configured.")
        return {"error": "[Error: Gemini API keys not configured]"}
    if not config.GEMINI_ASK_ALL_MODELS:
        logger.warning("No models configured in 'gemini_ask_all_models'.")
        return {"error": "[Error: No models configured for concurrent generation]"}

    current_key = _get_next_key()
    if not current_key:
         return {"error": "[Error: No Gemini keys available]"}

    logger.info(f"Sending concurrent requests to Gemini models: {config.GEMINI_ASK_ALL_MODELS} using Key Index: {_current_key_index}")

    tasks = []
    for model_name in config.GEMINI_ASK_ALL_MODELS:
        # Ensure the correct model name format is used if needed (e.g., 'models/...')
        # Assuming model names in config are already correct for the API
        tasks.append(
            _generate_single_model_response(model_name, prompt, context_history, current_key)
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    response_dict = {}
    for model_name, result in zip(config.GEMINI_ASK_ALL_MODELS, results):
        if isinstance(result, Exception):
            logger.error(f"Exception during concurrent generation for model {model_name}: {result}")
            response_dict[model_name] = f"[Unhandled Exception: {result}]"
        else:
            response_dict[model_name] = result

    return response_dict

# Example usage (for testing purposes)
async def _test():
    print("Testing Gemini Service...")
    if not await check_connection():
        print("Gemini connection failed or not configured. Exiting test.")
        return

    test_model_single = config.DEFAULT_GEMINI_MODEL
    print(f"\nTesting single response streaming with model: {test_model_single}")
    prompt_single = "Why is the sky blue?"
    print(f"Prompt: {prompt_single}")
    full_response_single = ""
    async for chunk in generate_response(model=test_model_single, prompt=prompt_single):
        print(chunk, end="", flush=True)
        full_response_single += chunk
    print("\n--- End of Single Generation ---")

    if config.GEMINI_ASK_ALL_MODELS:
        print(f"\nTesting concurrent generation with models: {config.GEMINI_ASK_ALL_MODELS}")
        prompt_concurrent = "Write a short poem about a cat."
        print(f"Prompt: {prompt_concurrent}")
        concurrent_results = await generate_concurrent_responses(prompt=prompt_concurrent)
        print("\n--- Concurrent Results ---")
        for model, response in concurrent_results.items():
            print(f"--- Model: {model} ---")
            print(response)
            print("-" * (len(model) + 14))
        print("--- End of Concurrent Results ---")
    else:
        print("\nSkipping concurrent generation test: No models configured in 'gemini_ask_all_models'.")

if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()
    import config # Re-import after loading .env
    _gemini_keys = config.GEMINI_API_KEYS
    _key_iterator = cycle(_gemini_keys) if _gemini_keys else None
    _current_key_index = 0

    if _gemini_keys:
        asyncio.run(_test())
    else:
        print("No Gemini API keys found in config for testing.")

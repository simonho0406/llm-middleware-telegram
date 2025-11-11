import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
import logging
import asyncio
import config
from typing import List, Dict, Optional, AsyncGenerator, Any

logger = logging.getLogger(__name__)

# --- Service Functions ---

async def generate_response(model: str, prompt: str, context_history: Optional[List[Dict]] = None, request_timeout: int = None) -> AsyncGenerator[str, None]:
    """Generates a response from Gemini with request-scoped key rotation for rate-limiting."""
    keys = config.GEMINI_API_KEYS
    if not keys:
        logger.warning("No Gemini API keys configured.")
        yield "[Error: Gemini API keys not configured]"
        return

    gemini_history = []
    if context_history:
        for msg in context_history:
            role = 'user' if msg.get('role') == 'user' else 'model'
            content = msg.get('content', '')
            gemini_history.append({'role': role, 'parts': [content]})
    
    full_prompt = gemini_history + [{'role': 'user', 'parts': [prompt]}]

    for i, key in enumerate(keys):
        try:
            logger.info(f"Attempting Gemini request with Key Index: {i}")
            genai.configure(api_key=key)
            gemini_model = genai.GenerativeModel(model)
            
            response_stream = await gemini_model.generate_content_async(
                contents=full_prompt,
                stream=True,
                request_options={'timeout': request_timeout or config.get_request_timeout_seconds()}
            )

            async for chunk in response_stream:
                if chunk.prompt_feedback.block_reason:
                    logger.warning(f"Gemini content blocked (Key Index: {i}, Reason: {chunk.prompt_feedback.block_reason})")
                    yield f"[Error: Content blocked by Gemini - {chunk.prompt_feedback.block_reason}]"
                    return
                if hasattr(chunk, 'text'):
                    yield chunk.text
            
            logger.info(f"Gemini stream finished successfully with Key Index: {i}")
            return # Success, so we exit the function

        except google_exceptions.ResourceExhausted as e:
            logger.warning(f"Gemini key at index {i} is rate-limited, trying next key. Reason: {e}")
            continue # Go to the next key

        except Exception as e:
            logger.error(f"A non-recoverable Gemini error occurred with Key Index {i} (Model: {model}): {e}")
            yield f"[Error: A critical error occurred with the Gemini API: {e}]"
            return # Stop on other errors

    logger.error("All Gemini API keys are rate-limited or failing.")
    yield "[Error: All Gemini API keys are currently rate-limited or failing.]"

async def list_models() -> List[Dict[str, Any]]:
    """Lists available Gemini models by trying all available keys until one succeeds."""
    keys = config.GEMINI_API_KEYS
    if not keys:
        logger.warning("Cannot list Gemini models: No API keys configured.")
        return []

    for i, key in enumerate(keys):
        try:
            genai.configure(api_key=key)
            all_models_iter = await asyncio.to_thread(genai.list_models)
            
            generative_models = [
                {"id": m.name.split('/')[-1], "name": m.display_name}
                for m in all_models_iter
                if 'generateContent' in m.supported_generation_methods
            ]
            
            logger.info(f"Successfully listed {len(generative_models)} models with Key Index: {i}.")
            return generative_models
        except Exception as e:
            logger.warning(f"Failed to list models with Key Index {i}: {e}")
            continue
    
    logger.error("Failed to list models with any of the provided Gemini keys.")
    return []

async def generate_concurrent_responses(prompt: str, context_history: Optional[List[Dict]] = None) -> Dict[str, str]:
    """Generates responses from multiple configured Gemini models concurrently."""
    keys = config.GEMINI_API_KEYS
    if not keys:
        return {"error": "[Error: Gemini API keys not configured]"}
    if not config.get_gemini_ask_all_models():
        return {"error": "[Error: No models configured for concurrent generation]"}

    working_key = None
    for i, key in enumerate(keys):
        try:
            genai.configure(api_key=key)
            await asyncio.to_thread(genai.list_models)
            logger.info(f"Found working Gemini key at index {i} for concurrent requests.")
            working_key = key
            break
        except Exception:
            continue
    
    if not working_key:
        logger.error("No working Gemini key found for concurrent requests.")
        return {model: "[Error: No available API keys]" for model in config.get_gemini_ask_all_models()}

    async def _concurrent_task(model_name: str) -> str:
        try:
            gemini_model = genai.GenerativeModel(model_name)
            full_prompt = (context_history or []) + [{'role': 'user', 'parts': [prompt]}]
            response = await asyncio.wait_for(
                asyncio.to_thread(gemini_model.generate_content, contents=full_prompt),
                timeout=config.get_request_timeout_seconds()
            )
            return response.text.strip() if hasattr(response, 'text') else "[Empty Response]"
        except Exception as e:
            logger.error(f"Error during concurrent Gemini generation for model {model_name}: {e}")
            return f"[Error: {e}]"

    tasks = [_concurrent_task(model) for model in config.get_gemini_ask_all_models()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return {model: res for model, res in zip(config.get_gemini_ask_all_models(), results)}

# Example usage (for testing purposes)
async def _test():
    print("Testing Gemini Service...")
    # This test function is now simplified as check_connection is removed.
    # A simple call to list_models can serve as a connection check.
    print("Attempting to list models to check connection...")
    models = await list_models()
    if not models:
        print("Gemini connection failed or no models found.")
        return
    print(f"Found {len(models)} models. Connection successful.")

    test_model_single = config.get_default_gemini_model()
    print(f"\nTesting single response streaming with model: {test_model_single}")
    prompt_single = "Why is the sky blue?"
    print(f"Prompt: {prompt_single}")
    full_response_single = ""
    async for chunk in generate_response(model=test_model_single, prompt=prompt_single):
        print(chunk, end="", flush=True)
        full_response_single += chunk
    print("\n--- End of Single Generation ---")

    if config.get_gemini_ask_all_models():
        print(f"\nTesting concurrent generation with models: {config.get_gemini_ask_all_models()}")
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
    import config
    if config.GEMINI_API_KEYS:
        asyncio.run(_test())
    else:
        print("No Gemini API keys found in config for testing.")
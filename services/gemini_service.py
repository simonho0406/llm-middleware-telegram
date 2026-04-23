import logging
import asyncio
import config
from typing import List, Dict, Optional, AsyncGenerator, Any
from google import genai
from google.genai import types
from google.genai import errors as google_exceptions

logger = logging.getLogger(__name__)

class GeminiService:
    def __init__(self, api_keys: Optional[List[str]] = None):
        """Initializes the Gemini service with a list of API keys for rate-limit rotation."""
        self.api_keys = api_keys if api_keys is not None else config.GEMINI_API_KEYS
        if not self.api_keys:
            logger.warning("GeminiService initialized with no API keys.")

    async def generate_response(self, model: str, prompt: str, context_history: Optional[List[Dict]] = None, request_timeout: int = None) -> AsyncGenerator[str, None]:
        """Generates a streaming response using instance-scoped clients."""
        if not self.api_keys:
            yield "[Error: Gemini API keys not configured]"
            return

        # Format history for v2 SDK: {'role': 'user', 'parts': [{'text': '...'}]}
        gemini_history = []
        if context_history:
            for msg in context_history:
                role = 'user' if msg.get('role') == 'user' else 'model'
                content = msg.get('content', '')
                gemini_history.append({'role': role, 'parts': [{'text': content}]})
        
        full_prompt = gemini_history
        if prompt:
            full_prompt.append({'role': 'user', 'parts': [{'text': prompt}]})
            
        generation_config = types.GenerateContentConfig(
            max_output_tokens=config.get_gemini_max_output_tokens()
        )

        for i, key in enumerate(self.api_keys):
            try:
                logger.info(f"Attempting Gemini request with Key Index: {i}")
                # Create a local client instance to avoid global state races
                client = genai.Client(api_key=key)
                
                # We do not explicitly pass a timeout config because the v2 SDK aio methods
                # expect http_options={'timeout': ...} on client initialization.
                # If needed, it can be added to the client instantiation.

                response_stream = await client.aio.models.generate_content_stream(
                    model=model,
                    contents=full_prompt,
                    config=generation_config
                )

                async for chunk in response_stream:
                    if hasattr(chunk, 'text') and chunk.text:
                        yield chunk.text
                    elif chunk.candidates and chunk.candidates[0].finish_reason:
                        reason = chunk.candidates[0].finish_reason
                        if reason != 'STOP':
                            logger.warning(f"Gemini content blocked/stopped (Key Index: {i}, Reason: {reason})")
                            yield f"[Error: Content blocked or improperly stopped by Gemini - {reason}]"
                            return
                
                logger.info(f"Gemini stream finished successfully with Key Index: {i}")
                return # Success, exit function

            except google_exceptions.APIError as e:
                # The v2 SDK uses APIError. We can inspect the status.
                if "429" in str(e) or "quota" in str(e).lower() or "exhausted" in str(e).lower():
                    logger.warning(f"Gemini key at index {i} is rate-limited, trying next key. Reason: {e}")
                    continue
                else:
                    logger.exception(f"A non-recoverable Gemini error occurred with Key Index {i} (Model: {model}): {e}")
                    yield f"[Error: A critical error occurred with the Gemini API: {e}]"
                    return
            except Exception as e:
                # Fallback for unexpected exceptions
                if "429" in str(e) or "exhausted" in str(e).lower():
                    logger.warning(f"Gemini key at index {i} is rate-limited, trying next key. Reason: {e}")
                    continue
                logger.exception(f"An unexpected Gemini error occurred with Key Index {i} (Model: {model}): {e}")
                yield f"[Error: A critical error occurred with the Gemini API: {e}]"
                return

        logger.error("All Gemini API keys are rate-limited or failing.")
        yield "[Error: All Gemini API keys are currently rate-limited or failing.]"

    async def list_models(self) -> List[Dict[str, Any]]:
        """Lists available Gemini models using the first working key."""
        if not self.api_keys:
            logger.warning("Cannot list Gemini models: No API keys configured.")
            return []

        for i, key in enumerate(self.api_keys):
            try:
                client = genai.Client(api_key=key)
                # v2 SDK: client.models.list()
                models_iter = await asyncio.to_thread(client.models.list)
                
                generative_models = [
                    {"id": m.name.split('/')[-1] if '/' in m.name else m.name, "name": m.display_name}
                    for m in models_iter
                    if 'generateContent' in m.supported_actions
                ]
                logger.info(f"Successfully listed {len(generative_models)} models with Key Index: {i}.")
                return generative_models
            except Exception as e:
                logger.exception(f"Failed to list models with Key Index {i}: {e}")
                continue
        
        logger.error("Failed to list models with any of the provided Gemini keys.")
        return []

    async def check_status(self) -> tuple[bool, str]:
        """Checks the status of the Gemini API by verifying key configuration and attempting to list models."""
        if not self.api_keys:
            return False, "Not configured (missing API keys)"
            
        try:
            models = await self.list_models()
            if models:
                return True, f"Online ({len(models)} models available, {len(self.api_keys)} keys active)"
            else:
                return False, "Offline (Failed to connect or list models)"
        except Exception as e:
            return False, f"Error connecting: {e}"

    async def generate_concurrent_responses(self, prompt: str, context_history: Optional[List[Dict]] = None) -> Dict[str, str]:
        """Generates responses from multiple configured Gemini models concurrently."""
        if not self.api_keys:
            return {"error": "[Error: Gemini API keys not configured]"}
        
        ask_models = config.get_gemini_ask_all_models()
        if not ask_models:
            return {"error": "[Error: No models configured for concurrent generation]"}

        working_key = None
        for i, key in enumerate(self.api_keys):
            try:
                client = genai.Client(api_key=key)
                await asyncio.to_thread(client.models.list)
                logger.info(f"Found working Gemini key at index {i} for concurrent requests.")
                working_key = key
                break
            except Exception:
                continue
        
        if not working_key:
            logger.exception("No working Gemini key found for concurrent requests.")
            return {model: "[Error: No available API keys]" for model in ask_models}

        # Create localized client for this batch
        batch_client = genai.Client(api_key=working_key)
        
        full_prompt = []
        if context_history:
            for msg in context_history:
                role = 'user' if msg.get('role') == 'user' else 'model'
                content = msg.get('content', '')
                full_prompt.append({'role': role, 'parts': [{'text': content}]})
        full_prompt.append({'role': 'user', 'parts': [{'text': prompt}]})
        
        generation_config = types.GenerateContentConfig(
             max_output_tokens=config.get_gemini_max_output_tokens()
        )

        async def _concurrent_task(model_name: str) -> str:
            try:
                response = await asyncio.wait_for(
                    batch_client.aio.models.generate_content(
                        model=model_name,
                        contents=full_prompt,
                        config=generation_config
                    ),
                    timeout=config.get_request_timeout_seconds()
                )
                return response.text.strip() if hasattr(response, 'text') and response.text else "[Empty Response]"
            except Exception as e:
                logger.exception(f"Error during concurrent Gemini generation for model {model_name}: {e}")
                return f"[Error: {e}]"

        tasks = [_concurrent_task(model) for model in ask_models]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {model: (res if not isinstance(res, Exception) else f"[Exception: {res}]") 
                for model, res in zip(ask_models, results)}

    async def _generate_single_model_non_streaming(self, model_id: str, prompt: str, context_history: Optional[List[Dict]] = None) -> str:
        """Internal helper to generate a response from a single model non-streamingly."""
        full_response = ""
        try:
            async for chunk in self.generate_response(model=model_id, prompt=prompt, context_history=context_history):
                full_response += chunk
            return full_response.strip()
        except Exception as e:
            logger.exception(f"Error in _generate_single_model_non_streaming for {model_id}: {e}")
            return f"[Error: {e}]"
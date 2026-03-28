import logging
import httpx
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, APIStatusError, APIError
import config
import asyncio
import tiktoken
import backoff
from bot.errors import ProviderUnavailableError

logger = logging.getLogger(__name__)

class OpenAICompatibleService:
    def __init__(self, provider_config: dict):
        if not all(k in provider_config for k in ['name', 'base_url', 'api_key', 'default_model']):
            raise ValueError(f"Invalid provider config passed to OpenAICompatibleService: {provider_config}")

        self.provider_name = provider_config['name']
        self.base_url = provider_config['base_url']
        self.api_key = provider_config['api_key']
        self.default_model = provider_config['default_model']
        self.allowed_models = provider_config.get('allowed_models', [])
        if not self.allowed_models:
            logger.warning(f"Allowed models not configured for provider '{self.provider_name}'. Using default empty list.")

        self.api_version = provider_config.get('api_version')

        try:
            self.client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                http_client=httpx.AsyncClient(timeout=config.get_request_timeout_seconds()),
                default_headers={"OpenAI-Version": self.api_version} if self.api_version else None,
                max_retries=0, # Disable library-level retries; rely on app-level logic
            )
            logger.info(f"OpenAICompatibleService initialized for provider '{self.provider_name}' with base URL '{self.base_url}'")
        except Exception as e:
            logger.exception(f"Failed to initialize AsyncOpenAI client for {self.provider_name}: {e}")
            self.client = None

        self.max_retries = provider_config.get('max_retries', 3)
        self.initial_delay = provider_config.get('initial_delay', 1)
        self.enable_streaming = provider_config.get('enable_streaming', True)  # Allow disabling streaming per provider

    async def close(self):
        """Explicitly close the underlying HTTP client."""
        if self.client:
            await self.client.close()
            logger.info(f"Closed OpenAICompatibleService client for '{self.provider_name}'")

    async def list_models(self) -> list[str]:
        if not hasattr(self.client.models, 'list'):
            logger.info(f"Dynamic model listing not supported for '{self.provider_name}'. Using pre-configured models.")
            return self.allowed_models
        if not self.client:
            logger.warning(f"Cannot list models for '{self.provider_name}': client not initialized.")
            return self.allowed_models

        try:
            models_response = await self.client.models.list()
            if models_response and hasattr(models_response, 'data'):
                model_ids = [model.id for model in models_response.data if hasattr(model, 'id')]
                if model_ids:
                    logger.info(f"Fetched {len(model_ids)} models dynamically from '{self.provider_name}'.")
                    return model_ids
                else:
                    logger.warning(f"API call to list models for '{self.provider_name}' returned no model IDs.")
            else:
                logger.warning(f"Unexpected response format when listing models for '{self.provider_name}'. Response: {models_response}") # Log the response
        except APIStatusError as e:
            # Log the full error details
            logger.warning(f"API Status Error listing models for '{self.provider_name}' (Status {e.status_code}): {e}. Falling back to configured list.")
        except NotImplementedError as e:
             # Log the error itself
            logger.warning(f"Listing models is not implemented by the API for '{self.provider_name}': {e}. Falling back to configured list.")
        except APIConnectionError as e:
            # Catch connection errors specifically
            logger.error(f"API Connection Error listing models for '{self.provider_name}': {e}. Falling back to configured list.")
        except Exception as e:
            # Log the full exception for unexpected errors
            logger.exception(f"Unexpected error listing models for '{self.provider_name}': {e}. Falling back to configured list.")

        logger.info(f"Falling back to configured 'allowed_models' for '{self.provider_name}'.")
        return self.allowed_models

    async def generate_response(self, model: str, prompt: str, context_history: list = None, request_timeout: int = None):
        if not self.client:
            yield f"[Error: Client for provider '{self.provider_name}' not initialized]"
            return

        messages = []
        if context_history:
            # Sanitize roles for strict APIs (e.g. NVIDIA)
            # Map 'assistant:panel' -> 'assistant', leave others as is
            for msg in context_history:
                role = msg.get("role", "")
                content = msg.get("content", "")
                
                # strict validation fix: map internal roles to API-allowed roles
                if role == "assistant:panel":
                    role = "assistant"
                    
                messages.append({"role": role, "content": content})
                
        if prompt:
            messages.append({"role": "user", "content": prompt})

        logger.debug(f"[{self.provider_name}] Sending request to model '{model}' with {len(messages)} messages.")
        retries = self.max_retries
        delay = self.initial_delay
        success = False

        for attempt in range(retries + 1):
            try:
                # Prepare keyword arguments for the API call
                use_streaming = self.enable_streaming and True  # Can be overridden by individual request if needed
                api_kwargs = {
                    "model": model,
                    "messages": messages,
                    "stream": use_streaming,
                }
                # Use per-request timeout or fallback to global default
                timeout_value = request_timeout if request_timeout is not None else config.get_request_timeout_seconds()
                api_kwargs["timeout"] = timeout_value

                # Targeted Reasoning Payload: Maximize test-time scaling globally.
                reasoning_payload = {
                    "reasoning_effort": "high"
                }
                
                # OpenRouter specifically supports including reasoning traces in the completion delta
                if "openrouter.ai" in self.base_url.lower():
                    reasoning_payload["include_reasoning"] = True
                
                # Attempt 1: Try with reasoning params (only if payload is not empty)
                try:
                    current_api_kwargs = api_kwargs.copy()
                    if attempt == 0 and reasoning_payload: # Only inject if we have specific params
                        current_api_kwargs["extra_body"] = reasoning_payload
                    
                    if use_streaming:
                        stream = await self.client.chat.completions.create(**current_api_kwargs)
                        async for chunk in stream:
                            if chunk and hasattr(chunk, 'choices') and chunk.choices:
                                content = chunk.choices[0].delta.content
                                if content is not None:
                                    yield content
                    else:
                        response = await self.client.chat.completions.create(**current_api_kwargs)
                        if response and hasattr(response, 'choices') and response.choices:
                            content = response.choices[0].message.content
                            if content:
                                yield content
                        else:
                            logger.exception(f"[{self.provider_name}] API returned empty/invalid response")
                            yield f"[Error: Provider returned invalid response format.]"
                            return
                    success = True
                    break

                except (APIStatusError) as e:
                    # Special handling for "Reasoning Not Supported" (400 Bad Request)
                    if e.status_code == 400 and attempt == 0:
                        logger.warning(f"[{self.provider_name}] Model rejected reasoning parameters (400 Bad Request). Retrying without reasoning...")
                        # We do NOT increment attempt counter for this fallback, or we can just continue to next logic.
                        # But wait, we need to retry *immediately* without params.
                        # Ideally we do this in a nested structure, but here we can just "continue" if we ensure next loop doesn't use params.
                        # Actually, my logic above `if attempt == 0` handles this! 
                        # If we trigger `continue`, next attempt is 1. `attempt == 0` will be false. Params won't be added.
                        # So we essentially just treat this as a "failed attempt" that consumes 1 retry quota.
                        # Given we have 3 retries, this is acceptable.
                        pass 
                    elif "EngineCore" in str(e):
                        logger.error(f"[{self.provider_name}] NVIDIA EngineCore Error: {e}")
                        raise ProviderUnavailableError("NVIDIA service unavailable") from e
                    else:
                        logger.error(f"[{self.provider_name}] API Status Error: {e.status_code} - {e.response}")
                        yield f"[Error: API returned an error (Status {e.status_code}). Details: {e.message}]"
                        return

            except (APIConnectionError, RateLimitError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                if attempt < retries:
                    logger.warning(f"[{self.provider_name}] API Error: {e}. Retrying in {delay} seconds. (Attempt {attempt + 1}/{retries})")
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error(f"[{self.provider_name}] API Error: {e}. No more retries left.")
                    yield f"[Error: Failed to connect after {retries} retries. Details: {e}]"
            except APIError as e:
                # Handle general API errors including streaming errors
                error_message = str(e)
                if "streaming" in error_message.lower() and use_streaming:
                    logger.warning(f"[{self.provider_name}] Streaming error occurred: {e}")
                    # Try non-streaming mode as fallback only if we were using streaming
                    try:
                        logger.info(f"[{self.provider_name}] Attempting fallback to non-streaming mode...")
                        api_kwargs["stream"] = False
                        response = await self.client.chat.completions.create(**api_kwargs)
                        if response and hasattr(response, 'choices') and response.choices:
                            content = response.choices[0].message.content
                            if content:
                                yield content
                                success = True
                                break
                        else:
                            logger.error(f"[{self.provider_name}] Non-streaming fallback returned empty/invalid response")
                            yield f"[Error: Provider returned invalid response format.]"
                            return
                    except Exception as fallback_error:
                        logger.exception(f"[{self.provider_name}] Non-streaming fallback also failed: {fallback_error}")
                        yield f"[Error: Both streaming and non-streaming failed. Provider may be temporarily unavailable.]"
                        return
                else:
                    logger.error(f"[{self.provider_name}] API Error: {e}")
                    yield f"[Error: API error occurred. Details: {e}]"
                    return
            except Exception as e:
                if "EngineCore" in str(e):
                    logger.exception(f"[{self.provider_name}] NVIDIA EngineCore Error: {e}")
                    raise ProviderUnavailableError("NVIDIA service unavailable") from e
                else:
                    logger.exception(f"[{self.provider_name}] Unexpected error during generation: {e}")
                    yield f"[Error: An unexpected error occurred with the {self.provider_name} provider. Details: {e}]"
                return

        if not success:
            yield f"[Error: Failed to generate response after {retries} retries.]"

    def get_allowed_models(self) -> list[str]:
        return self.allowed_models

    def get_default_model(self) -> str:
        return self.default_model

    async def check_status(self) -> (bool, str):
        """Checks if the provider is configured."""
        is_configured = bool(self.api_key and self.api_key != "YOUR_API_KEY")
        message = "API key is configured." if is_configured else "API key is not configured."
        return is_configured, message

    async def count_tokens(self, content: list) -> int:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return sum(len(encoding.encode(c["content"])) for c in content)
        except ImportError:
            logger.warning("tiktoken not installed, falling back to word-based estimation")
            return sum(len(c["content"].split()) for c in content)

    async def _generate_single_model_non_streaming(self, model: str, prompt: str, context_history: list = None) -> str:
        """Helper to generate a complete string response using the existing generator logic."""
        full_response = ""
        try:
            async for chunk in self.generate_response(model, prompt, context_history):
                full_response += chunk
        except Exception as e:
            logger.exception(f"Error in non-streaming generation for {self.provider_name}/{model}: {e}")
            return f"[Error: {str(e)}]"
        
        return full_response.strip() if full_response else "[Error: Empty response]"

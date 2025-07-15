import logging
import httpx
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, APIStatusError
import config
import asyncio
import tiktoken

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

        try:
            self.client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                http_client=httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT_SECONDS),
            )
            logger.info(f"OpenAICompatibleService initialized for provider '{self.provider_name}' with base URL '{self.base_url}'")
        except Exception as e:
            logger.error(f"Failed to initialize AsyncOpenAI client for {self.provider_name}: {e}")
            self.client = None

        self.max_retries = provider_config.get('max_retries', 3)
        self.initial_delay = provider_config.get('initial_delay', 1)

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
            messages.extend(context_history)
        messages.append({"role": "user", "content": prompt})

        logger.debug(f"[{self.provider_name}] Sending request to model '{model}' with {len(messages)} messages.")
        retries = self.max_retries
        delay = self.initial_delay
        success = False

        for attempt in range(retries + 1):
            try:
                # Prepare keyword arguments for the API call
                api_kwargs = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                }
                if request_timeout is not None:
                    api_kwargs["timeout"] = request_timeout

                stream = await self.client.chat.completions.create(**api_kwargs)
                async for chunk in stream:
                    content = chunk.choices[0].delta.content
                    if content is not None:
                        yield content
                success = True
                break
            except (APIConnectionError, RateLimitError) as e:
                if attempt < retries:
                    logger.error(f"[{self.provider_name}] API Error: {e}. Retrying in {delay} seconds.")
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error(f"[{self.provider_name}] API Error: {e}. No more retries left.")
                    yield f"[Error: Failed to connect after {retries} retries. Details: {e}]"
            except APIStatusError as e:
                logger.error(f"[{self.provider_name}] API Status Error: {e.status_code} - {e.response}")
                yield f"[Error: API returned an error (Status {e.status_code}). Details: {e.message}]"
                return
            except Exception as e:
                logger.exception(f"[{self.provider_name}] Unexpected error during generation: {e}")
                yield f"[Error: An unexpected error occurred with the {self.provider_name} provider. Details: {e}]"
                return

        if not success:
            yield f"[Error: Failed to generate response after {retries} retries.]"

    def get_allowed_models(self) -> list[str]:
        return self.allowed_models

    def get_default_model(self) -> str:
        return self.default_model

    async def count_tokens(self, content: list) -> int:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return sum(len(encoding.encode(c["content"])) for c in content)
        except ImportError:
            logger.warning("tiktoken not installed, falling back to word-based estimation")
            return sum(len(c["content"].split()) for c in content)

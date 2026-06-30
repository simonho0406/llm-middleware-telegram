import logging
import os
import config
from services import ollama_service, gemini_service
from services.openai_compatible_service import OpenAICompatibleService

logger = logging.getLogger(__name__)

# Store initialized service instances to avoid re-creating them constantly
# For modules like ollama_service, we store the module itself.
# For class-based services like OpenAICompatibleService, we store instances.
_initialized_services = {}
_provider_details_cache = None

def get_provider_details() -> dict:
    """
    Initializes all provider services based on config and returns a dictionary
    mapping provider names to their details.

    Returns:
        dict: {
            'provider_name': {
                'service': service_instance_or_module,
                'model_session_key': str, # e.g., 'ollama_model', 'groq_model'
                'default_model': str,
                'allowed_models': list[str] # From config or service
            },
            ...
        }
    """
    global _provider_details_cache
    if _provider_details_cache is not None:
        return _provider_details_cache

    details = {}
    # Track which services we add to the module-level _initialized_services during
    # THIS attempt. On failure mid-init we pop them out so a subsequent retry
    # doesn't see "leftover" partial state and skip building fresh instances —
    # the old instances would be orphaned, holding httpx pools (see ticket 029).
    _added_this_attempt: list[str] = []

    try:
        _build_provider_details(details, _added_this_attempt)
    except Exception:
        for name in _added_this_attempt:
            _initialized_services.pop(name, None)
        raise

    if not details:
         logger.critical("No valid LLM providers were configured or initialized!")

    _provider_details_cache = details
    logger.info(f"Initialized provider details for: {list(details.keys())}")
    return details


def _build_provider_details(details: dict, _added_this_attempt: list[str]) -> None:
    """Body of get_provider_details(), extracted so the try/except wrapper is small."""
    # --- Built-in Providers ---
    # Ollama
    details['ollama'] = {
        'service': ollama_service,
        'default_model': config.get_default_ollama_model(),
        'allowed_models': []
    }
    if 'ollama' not in _initialized_services:
        _initialized_services['ollama'] = ollama_service
        _added_this_attempt.append('ollama')
    # Gemini
    if config.GEMINI_API_KEYS:
        gemini_instance = gemini_service.GeminiService()
        details['gemini'] = {
            'service': gemini_instance,
            'default_model': config.get_default_gemini_model(),
            'allowed_models': config.get_gemini_ask_all_models()
        }
        if 'gemini' not in _initialized_services:
            _initialized_services['gemini'] = gemini_instance
            _added_this_attempt.append('gemini')
    else:
        logger.warning("Gemini provider disabled: No API keys found.")

    # OpenRouter — uses OpenAICompatibleService (same wire protocol, tool-call capable)
    if config.OPENROUTER_API_KEY and config.OPENROUTER_API_KEY != "YOUR_OPENROUTER_API_KEY":
        openrouter_conf = {
            'name': 'openrouter',
            'base_url': 'https://openrouter.ai/api/v1',
            'api_key': config.OPENROUTER_API_KEY,
            'default_model': config.get_default_openrouter_model(),
            'allowed_models': config.get_openrouter_allowed_models(),
        }
        openrouter_instance = OpenAICompatibleService(openrouter_conf)
        if openrouter_instance.client:
            details['openrouter'] = {
                'service': openrouter_instance,
                'default_model': config.get_default_openrouter_model(),
                'allowed_models': config.get_openrouter_allowed_models(),
                'enable_streaming': True,
            }
            if 'openrouter' not in _initialized_services:
                _initialized_services['openrouter'] = openrouter_instance
                _added_this_attempt.append('openrouter')
            logger.info("OpenRouter provider initialized via OpenAICompatibleService.")
        else:
            logger.warning("OpenRouter provider disabled: client failed to initialize.")
    else:
        logger.warning("OpenRouter provider disabled: API key not set.")

    # --- Custom OpenAI-Compatible Providers ---
    for provider_conf in config.get_custom_providers_config():
        name = provider_conf['name']
        if name in details:
            logger.warning(f"Custom provider name '{name}' conflicts with a built-in provider. Skipping.")
            continue

        if name not in _initialized_services:
            try:
                env_var_override = provider_conf.get('api_key')
                default_env_var = f"{name.upper()}_API_KEY"
                # config.get_env strips surrounding quotes/whitespace so a quoted .env
                # (which Docker's env_file passes literally) doesn't corrupt the key.
                provider_conf['api_key'] = config.get_env(env_var_override) if env_var_override and config.get_env(env_var_override) else config.get_env(default_env_var)
                if not provider_conf['api_key']:
                    logger.warning(f"API key environment variable for custom provider '{name}' not found. Skipping.")
                    continue

                service_instance = OpenAICompatibleService(provider_conf)
                if service_instance.client:
                    _initialized_services[name] = service_instance
                    _added_this_attempt.append(name)
                else:
                    logger.error(f"Failed to initialize client for custom provider '{name}'. Skipping.")
                    continue
            except Exception as e:
                logger.exception(f"Failed to initialize service instance for custom provider '{name}': {e}. Skipping.")
                continue

        if name in _initialized_services:
             details[name] = {
                'service': _initialized_services[name],
                'default_model': provider_conf['default_model'],
                'allowed_models': provider_conf.get('allowed_models', [])
            }

def get_available_provider_names() -> list[str]:
    """Returns a list of names for all successfully initialized providers."""
    return list(get_provider_details().keys())

def get_service_for_provider(provider_name: str):
    """Gets the service instance/module for a given provider name."""
    if provider_name == 'test':
        from unittest.mock import MagicMock
        return MagicMock()
    details = get_provider_details().get(provider_name)
    return details['service'] if details else None

def get_config_for_provider(provider_name: str) -> dict | None:
     """Gets the configuration dictionary for a given provider name."""
     return get_provider_details().get(provider_name)
 
async def shutdown_providers():
     """Gracefully closes all initialized provider services.

     CRITICAL: also resets `_provider_details_cache`. The cache holds service
     instances whose internal httpx.AsyncClient connection pools are bound to
     the *current* asyncio event loop. main.py's polling loop creates a new
     event loop on each restart after a NetworkError — if we kept the cache,
     the next iteration would return services bound to a closed loop, and
     every chat request would raise "RuntimeError: Event loop is closed".
     """
     global _initialized_services, _provider_details_cache

     if _initialized_services:
         logger.info("Shutting down LLM provider services...")
         for name, service in _initialized_services.items():
             if hasattr(service, 'close'):
                 try:
                     await service.close()
                     logger.debug(f"Closed service: {name}")
                 except Exception as e:
                     logger.exception(f"Error closing service '{name}': {e}")
         _initialized_services.clear()
         logger.info("All LLM providers shutdown.")

     # Always reset cache, even if _initialized_services was empty
     _provider_details_cache = None

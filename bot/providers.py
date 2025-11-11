import logging
import os
import config
from services import ollama_service, gemini_service, openrouter_service
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

    # --- Built-in Providers ---
    # Ollama
    details['ollama'] = {
        'service': ollama_service,
        'default_model': config.get_default_ollama_model(),
        'allowed_models': [] # Will be fetched dynamically via API if needed
    }
    # Gemini
    if config.GEMINI_API_KEYS: # Only add if keys are configured
        details['gemini'] = {
            'service': gemini_service,
            'default_model': config.get_default_gemini_model(),
            'allowed_models': config.get_gemini_ask_all_models() # Use ask_all list for selection
        }
    else:
        logger.warning("Gemini provider disabled: No API keys found.")
        
    # OpenRouter
    if config.OPENROUTER_API_KEY and config.OPENROUTER_API_KEY != "YOUR_OPENROUTER_API_KEY":
         details['openrouter'] = {
            'service': openrouter_service,
            'default_model': config.get_default_openrouter_model(),
            'allowed_models': config.get_openrouter_allowed_models()
        }
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
                # Add the fetched API key to the provider config before initialization
                provider_conf['api_key'] = os.getenv(f"{provider_conf['name'].upper()}_API_KEY")
                if not provider_conf['api_key']:
                    logger.warning(f"API key environment variable for custom provider '{name}' not found. Skipping.")
                    continue

                service_instance = OpenAICompatibleService(provider_conf)
                if service_instance.client: # Check if client initialized successfully
                    _initialized_services[name] = service_instance
                else:
                    logger.error(f"Failed to initialize client for custom provider '{name}'. Skipping.")
                    continue # Skip adding this provider if client failed
            except Exception as e:
                logger.error(f"Failed to initialize service instance for custom provider '{name}': {e}. Skipping.")
                continue # Skip adding this provider

        # Add details if service was initialized successfully
        if name in _initialized_services:
             details[name] = {
                'service': _initialized_services[name],
                'default_model': provider_conf['default_model'],
                'allowed_models': provider_conf.get('allowed_models', [])
            }

    if not details:
         logger.critical("No valid LLM providers were configured or initialized!")

    _provider_details_cache = details
    logger.info(f"Initialized provider details for: {list(details.keys())}")
    return details

def get_available_provider_names() -> list[str]:
    """Returns a list of names for all successfully initialized providers."""
    return list(get_provider_details().keys())

def get_service_for_provider(provider_name: str):
    """Gets the service instance/module for a given provider name."""
    details = get_provider_details().get(provider_name)
    return details['service'] if details else None

def get_config_for_provider(provider_name: str) -> dict | None:
     """Gets the configuration dictionary for a given provider name."""
     return get_provider_details().get(provider_name)

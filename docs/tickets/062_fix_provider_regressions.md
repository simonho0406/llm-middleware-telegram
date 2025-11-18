# TICKET-062: Fix Regressions in Provider Initialization and Model Listing

**Status:** Open
**Priority:** Critical
**User Story:** As a user, I expect all providers configured in `config.yaml` (like Groq and Nvidia) to be available, and I expect the OpenRouter provider to correctly list only free models, as it did before the refactoring.

## Root Cause Analysis
The architectural refactoring in TICKET-061, while beneficial, introduced two critical regressions by losing provider-specific logic.

1.  **Missing `groq` and `nvidia` Providers:** The new provider factory in `bot/providers.py` has flawed logic for retrieving API keys from environment variables. It correctly reads the `api_key_env` field from the config (e.g., `CUSTOM_PROVIDER_API_KEY_GROQ`) but fails to use it to load the key with `os.getenv()`, causing it to skip these providers.

2.  **Incorrect OpenRouter Model List:** The old `openrouter_service.py` contained specific logic to filter the `/models` list and return only those with a price of zero. This provider-specific logic was lost when we moved to the generic `OpenAICompatibleService`, which now returns all models from the API.

## Resolution Plan

### Part 1: Fix API Key Loading

1.  **Modify `bot/providers.py`:**
    *   In the `get_provider_details` function, locate the section that handles `openai_compatible` providers.
    *   Correct the logic to properly use the `api_key_env` value. The implementation should be:
        ```python
        api_key_env_var = conf.get('api_key_env')
        api_key = os.getenv(api_key_env_var) if api_key_env_var else None
        ```
    *   Ensure the subsequent check for a valid `api_key` works as expected.

### Part 2: Restore OpenRouter Free Model Filtering

We must fix this without making the `OpenAICompatibleService` specific to OpenRouter. The solution is to make this a configuration-driven, opt-in feature.

1.  **Update `config.yaml`:**
    *   In the `providers.openrouter` section, add a new boolean key:
        ```yaml
        openrouter:
          type: "openai_compatible"
          # ... other settings
          filter_free_models: true # New flag
        ```

2.  **Modify `services/openai_compatible_service.py`:**
    *   In the `__init__` method of the `OpenAICompatibleService` class, store the value of this new flag:
        ```python
        self.filter_free_models = provider_config.get('filter_free_models', False)
        ```
    *   In the `list_models` method, add the following logic right after successfully fetching the `models_data`:
        ```python
        if self.filter_free_models:
            logger.info(f"Filtering for free models on provider '{self.provider_name}'.")
            free_models = []
            for model in models_data:
                pricing = model.get("pricing", {})
                prompt_cost = float(pricing.get("prompt", "1"))
                completion_cost = float(pricing.get("completion", "1"))
                if prompt_cost == 0.0 and completion_cost == 0.0:
                    free_models.append(model)
            models_data = free_models # Replace the full list with the filtered list
            logger.info(f"Found {len(models_data)} free models for '{self.provider_name}'.")
        
        # The rest of the function proceeds as normal, using the (now possibly filtered) models_data
        model_ids = [model.id for model in models_data if hasattr(model, 'id')]
        ```

### Verification

1.  After implementing the fixes, restart the bot.
2.  Run `/provider_status` and confirm that `groq` and `nvidia` are now listed as configured.
3.  Run `/ask_selected`, choose `openrouter`, and confirm that the model list now only contains the free models.
4.  Run the entire `pytest` suite to ensure no new regressions have been introduced.

# TICKET-063: Fix Dynamic API Key Loading for Custom Providers

**Status:** Open
**Priority:** Critical
**User Story:** As a user, I expect all providers configured in `config.yaml` (like Groq and Nvidia) to be available, so I can use their models.

## Root Cause Analysis
The previous fix for the missing providers was incorrect. The true root cause is a subtle issue with how environment variables are loaded and accessed.

The `load_dotenv()` function is called only once in `config.py`. While this makes the variables available to be read *within that file at import time*, it does not reliably populate `os.environ` for subsequent `os.getenv()` calls made from other modules like `bot/providers.py`.

The `OPENROUTER_API_KEY` worked because it was loaded into a global variable `config.OPENROUTER_API_KEY`. The `groq` and `nvidia` keys failed because `bot/providers.py` was trying to dynamically look them up using `os.getenv()`, which was failing because the environment was not fully populated at that point in the execution.

## Resolution Plan
All environment variable loading MUST be centralized in `config.py`. Other modules should get configuration values from the `config` module, not by calling `os.getenv()` themselves.

### Part 1: Centralize Custom API Key Loading

1.  **Modify `config.py`:**
    *   After the initial `load_dotenv()` call, add new logic to read the custom provider API keys.
    *   This logic should iterate through the `custom_openai_providers` section of the loaded `_yaml_config`.
    *   For each provider, it should get the `api_key_env` variable name and use `os.getenv()` to load the key.
    *   Store these keys in a new global dictionary within the `config` module, for example: `CUSTOM_PROVIDER_API_KEYS = {}`.

    **Example implementation for `config.py`:**
    ```python
    # (After _yaml_config is loaded)
    CUSTOM_PROVIDER_API_KEYS = {}
    custom_providers = _yaml_config.get("custom_openai_providers", [])
    if isinstance(custom_providers, list):
        for provider in custom_providers:
            if isinstance(provider, dict) and 'name' in provider and 'api_key_env' in provider:
                key_name = provider['api_key_env']
                key_value = os.getenv(key_name)
                if key_value:
                    CUSTOM_PROVIDER_API_KEYS[provider['name']] = key_value
    ```
    *(Note: The ticket for `gemini-cli-dev` should be more precise, this is a conceptual example)*

### Part 2: Update Provider Factory to Use Centralized Keys

1.  **Modify `bot/providers.py`:**
    *   In the `get_provider_details` function, remove the call to `os.getenv()`.
    *   Instead, get the API key from the new dictionary in the `config` module:
        ```python
        # (Inside the loop for openai_compatible providers)
        api_key = config.CUSTOM_PROVIDER_API_KEYS.get(name)
        ```
    *   The rest of the logic for checking if the key exists can remain the same.

### Verification
1.  After implementing the fixes, restart the bot.
2.  Check the startup logs. The "API key for provider... not found" warnings for `groq` and `nvidia` MUST be gone.
3.  The `Initialized provider details for:` log message MUST now include `groq` and `nvidia`.
4.  Run the full `pytest` suite to ensure no regressions.

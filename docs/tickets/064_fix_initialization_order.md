# TICKET-064: Fix Circular Dependency/Initialization Order for API Keys

**Status:** Open
**Priority:** Blocker
**User Story:** As a user, I expect all providers configured in `config.yaml` (like Groq and Nvidia) to be available, so I can use their models.

## Root Cause Analysis
The current implementation fails due to a subtle Python initialization order problem.

1.  `config.py` loads the `.env` file and then immediately, at the module level (import time), iterates through the YAML config to populate a `CUSTOM_PROVIDER_API_KEYS` dictionary.
2.  `bot/providers.py` imports `config` and attempts to read this dictionary.
3.  Due to the complex chain of imports, `bot/providers.py` is being initialized and calls `config.CUSTOM_PROVIDER_API_KEYS` *before* the logic in `config.py` has had a chance to run and populate it. The dictionary is empty at the time it's accessed.

Running logic at the module level is fragile. The solution is to move this logic into a function that is called only when the data is first needed (lazy initialization).

## Resolution Plan

### Part 1: Implement Lazy Initialization in `config.py`

1.  **Modify `config.py`:**
    *   Remove the block of logic that populates `CUSTOM_PROVIDER_API_KEYS` from the global scope.
    *   Create a new function `get_custom_provider_api_keys()`.
    *   Inside this function, use a private global variable (e.g., `_custom_provider_api_keys`) and a memoization pattern to ensure the logic runs only once.

    **Example implementation for `config.py`:**
    ```python
    _custom_provider_api_keys = None

    def get_custom_provider_api_keys():
        """
        Loads API keys for custom providers from environment variables,
        defined in the YAML config. Uses memoization to run only once.
        """
        global _custom_provider_api_keys
        if _custom_provider_api_keys is not None:
            return _custom_provider_api_keys

        keys = {}
        if 'providers' in _yaml_config and isinstance(_yaml_config['providers'], dict):
            for provider_name, provider_config in _yaml_config['providers'].items():
                if isinstance(provider_config, dict) and 'api_key_env' in provider_config:
                    key_name = provider_config['api_key_env']
                    key_value = os.getenv(key_name)
                    if key_value:
                        keys[provider_name] = key_value
                        logger.info(f"Loaded API key for custom provider: {provider_name}")
                    else:
                        logger.warning(f"Environment variable '{key_name}' for provider '{provider_name}' not found.")
        
        _custom_provider_api_keys = keys
        return _custom_provider_api_keys
    ```

### Part 2: Update Provider Factory to Use the New Function

1.  **Modify `bot/providers.py`:**
    *   In the `get_provider_details` function, replace the direct dictionary access with a call to the new function.
    *   **Change this:**
        ```python
        api_key = config.CUSTOM_PROVIDER_API_KEYS.get(name)
        ```
    *   **To this:**
        ```python
        api_key = config.get_custom_provider_api_keys().get(name)
        ```

### Verification
1.  After implementing the fixes, restart the bot.
2.  Check the startup logs. The "API key for provider... not found" warnings for `groq` and `nvidia` MUST be gone.
3.  The `Initialized provider details for:` log message MUST now include `groq` and `nvidia`.
4.  Run the full `pytest` suite to ensure no regressions.

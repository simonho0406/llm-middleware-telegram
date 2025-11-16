# TICKET-061: Refactor for Centralized, Configuration-Driven Architecture

**Status:** Open
**Priority:** High
**User Story:** As a developer and administrator, I want all model and provider-specific behavior to be controlled via `config.yaml`, so that the application is flexible, maintainable, and adaptable to new models and services without requiring code changes.

## Root Cause Analysis
A codebase investigation revealed a systemic architectural flaw: configuration is decentralized and hardcoded across multiple files, rather than being managed by the central `config.yaml`. This makes the system brittle, difficult to update, and not future-proof.

**Key Issues to Address:**
1.  **Hardcoded Context Limits:** `utils/context_manager.py` uses a hardcoded `MODEL_CONTEXT_LIMITS` dictionary, ignoring the `default_max_context_tokens` and `context_token_output_buffer` settings in `config.yaml`.
2.  **Hardcoded Model/Provider Names:** Various files contain hardcoded model names (e.g., "gemini-1.5-flash-latest") and provider names, preventing user configuration.
3.  **Hardcoded `if/elif` Logic:** Multiple handlers use rigid `if/elif` blocks to switch behavior based on provider names, making it difficult to add new providers.

## Refactoring Plan

This refactoring will be done in several phases to ensure stability.

### Phase 1: Centralize All Model and Provider Configuration

1.  **Update `config.yaml`:**
    *   Create a new, comprehensive `providers` section.
    *   For each provider (`ollama`, `gemini`, `openrouter`, etc.), define a sub-section containing all its relevant settings: `default_model`, `max_context_tokens`, `output_buffer_tokens`, `allowed_models`, etc.
    *   Remove the old, scattered settings like `default_ollama_model`, `default_gemini_model`, etc.

2.  **Update `config.py`:**
    *   Create new, generic accessor functions to read from the new `providers` structure in `config.yaml`. For example, `get_provider_config(provider_name)`.
    *   Remove all old, provider-specific accessor functions.
    *   Ensure all default values are read from `config.yaml`, not hardcoded in the function signature.

### Phase 2: Refactor `utils/context_manager.py`

1.  **Remove Hardcoded Dictionaries:** Delete the `MODEL_CONTEXT_LIMITS` and `provider_defaults` dictionaries.
2.  **Implement Configuration-Driven Logic:**
    *   The `get_model_context_limits` function must be rewritten.
    *   It should now call `config.get_provider_config(provider)` to get the `max_context_tokens` and `output_buffer_tokens` for the current model and provider.
    *   The logic should gracefully handle cases where a specific model is not listed under a provider's `allowed_models`, falling back to the provider's default settings.

### Phase 3: Eliminate Hardcoded Logic in Handlers and Services

1.  **Refactor `bot/agent_utils.py`:**
    *   Modify the `is_search_required` function. Instead of hardcoding the `gemini` provider and model, it should use a new configuration section (e.g., `specialized_agents.search_planner`) to determine which model to use.

2.  **Refactor `services/web_search_service.py`:**
    *   Remove the `if/elif` block for `tavily` and `google`.
    *   Implement a simple factory pattern or dictionary lookup that uses the `web_search.provider` setting from `config.yaml` to dynamically select the correct search function.

3.  **Refactor `bot/handlers/ask_selected_handler.py`:**
    *   This is the most complex part. The `if/elif` blocks for different providers must be removed.
    *   The logic should be generalized. Instead of checking the provider name, it should check for provider *capabilities* as defined in the new `config.yaml` structure (e.g., `supports_model_listing`, `is_openai_compatible`). This will require significant logic changes to make the handler provider-agnostic.

### Phase 4: Final Cleanup and Verification

1.  **Remove Obsolete Code:** Delete all old configuration accessor functions from `config.py` that have been replaced.
2.  **Update All Tests:** All unit and integration tests must be updated to reflect the new configuration structure. Tests that relied on hardcoded values will need to be rewritten to use mocked configuration.
3.  **Full Regression Test:** Run the entire test suite to ensure all functionality remains intact.

## Instruction for `gemini-cli-dev`
This is a major architectural refactoring, not a simple bug fix. Proceed with extreme care and follow the phased approach outlined above. **Do not attempt to do this all in one commit.**

1.  Start with **Phase 1**. Get the configuration centralized first.
2.  Proceed to **Phase 2**. This is the most critical part for fixing the user-reported issue.
3.  Tackle **Phase 3** one file at a time.
4.  Ensure all tests are passing at the end of each phase.

This refactoring is essential for the long-term health and maintainability of the project.

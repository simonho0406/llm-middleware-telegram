# TICKET-048: Harden Ollama Service and Status Checks

**User Story:**
As a user running Ollama locally, I want the bot to reliably connect to my Ollama instance, even if I start the bot before the Ollama server is fully running, and I want clear feedback if a specific model is not available.

**The Problem:**
The current `ollama_service.py` is brittle:
1.  It initializes the client once at startup. If the Ollama server isn't running, the client is permanently `None`, and all future requests fail.
2.  The `check_connection()` function is unreliable as it depends on `list_models()`, which can fail for various reasons even if the server is running.
3.  There is no robust check to see if a *specific model* exists before trying to use it, leading to confusing errors.

**Acceptance Criteria:**
1.  **Dynamic Client Initialization:**
    - Refactor `ollama_service.py` to not use a global client.
    - A new function, e.g., `get_ollama_client()`, should be created that returns a fresh `ollama.AsyncClient` instance. This function will be called at the beginning of `generate_response` and `list_models`.

2.  **Robust Health Check:**
    - Create a new function `check_ollama_health(client)` that performs a quick and reliable health check.
    - This function should make a `GET` request to the Ollama base URL (e.g., `http://localhost:11434/`). A successful response (e.g., status 200 with "Ollama is running") indicates the server is up. This is faster and more reliable than listing models.

3.  **Model-Specific Validation:**
    - Create a new function `is_model_available(client, model_name)`.
    - This function will call `client.list()` and check if the requested `model_name` is present in the response. It must handle different API response formats for the model list (e.g., a root list vs. a `{"models": [...]}` object).
    - The `generate_response` function in `ollama_service.py` **must** call `is_model_available` before attempting to generate content. If the model is not available, it must immediately yield a user-friendly error string like `[Error: Model 'llama3:latest' is not available on the Ollama server.]`.

4.  **Update Provider Status Check:**
    - A new command `/provider_status` will be created (under a separate ticket, TICKET-049), but the underlying function in `ollama_service.py` should be implemented here.
    - Create `ollama_service.check_status()` which uses the new `check_ollama_health()` to report if the service is reachable.

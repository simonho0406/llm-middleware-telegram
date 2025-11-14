# TICKET-049: Implement Centralized Provider Status Command

**User Story:**
As a bot administrator, I want a simple command to check the status of all configured AI providers so I can quickly diagnose connection and configuration issues without needing to check logs.

**The Problem:**
When the bot fails to respond, it's difficult to know why. The issue could be a missing API key, an unreachable service (like Ollama), or a misconfiguration. There is no easy way for a user to self-diagnose these common problems.

**Acceptance Criteria:**
1.  **Create `/provider_status` Command:**
    - A new command, `/provider_status`, will be added to the bot. It should be accessible to all users.
    - This command will be added to `misc_commands.py` and registered in `main.py`.
    - It should also be documented in the `/help` command text.

2.  **Implement Status Check Logic:**
    - The command handler will iterate through all providers configured in the application (Ollama, Gemini, OpenRouter, and any custom OpenAI-compatible providers).
    - For each provider, it will call a standardized status-checking function. This requires creating a `check_status()` method or function for each service.
        - **Ollama:** `ollama_service.check_status()` (implemented in TICKET-048) will check if the base URL is reachable.
        - **Gemini:** `gemini_service.check_status()` will check if at least one `GEMINI_API_KEY` is present in the environment.
        - **OpenRouter:** `openrouter_service.check_status()` will check if the `OPENROUTER_API_KEY` is present.
        - **OpenAICompatibleService:** The `check_status()` method on the service instance will check if its specific `api_key` is present.

3.  **Format the Output:**
    - The command will reply with a single, cleanly formatted message.
    - The message will list each provider and its status.
    - Use icons for clarity (e.g., ✅ for "Configured and Ready", ❌ for "Not Configured or Unreachable").

**Example Output:**
```
Provider Status:

✅ Ollama: Service is reachable at http://localhost:11434
✅ Gemini: API keys are configured.
❌ OpenRouter: API key is not configured.
✅ Groq (Custom): API key is configured.
❌ My-Custom-Provider (Custom): API key is not configured.
```

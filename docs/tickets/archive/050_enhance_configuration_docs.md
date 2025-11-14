# TICKET-050: Enhance Environment and Configuration Documentation

**User Story:**
As a new user setting up the bot, I want a clear, well-commented `.env.example` file and updated README so that I can easily understand which API keys and settings are required and which are optional.

**The Problem:**
The current configuration process is opaque. It's not clear which environment variables are needed for which providers, where to get the keys, or how to configure custom providers. This creates a high barrier to entry for new users.

**Acceptance Criteria:**
1.  **Rewrite `.env.example`:**
    - The existing `.env.example` file will be completely replaced.
    - The new file will be structured with clear sections using comments (e.g., `# --- Core Bot Settings ---`, `# --- Ollama ---`, `# --- Google Gemini ---`, etc.).
    - Every variable will have a comment explaining:
        - What it does.
        - Whether it is required or optional.
        - An example value (e.g., `OLLAMA_HOST=http://localhost:11434`).
        - Where to obtain the key, if applicable.
    - It must include variables for all supported services: `TELEGRAM_BOT_TOKEN`, `OLLAMA_HOST`, `GEMINI_API_KEYS`, `OPENROUTER_API_KEY`, and placeholder variables for a generic OpenAI-compatible provider.

2.  **Clarify Custom Provider Configuration:**
    - The `.env.example` will include commented-out example variables for a custom provider, like `CUSTOM_PROVIDER_API_KEY` and `CUSTOM_PROVIDER_BASE_URL`.
    - The comments will explicitly state that these names are arbitrary and must match what is configured in `config.yaml`.
    - `config.yaml` will be updated with a commented-out example of a custom provider that corresponds to the new `.env.example` variables, making the link between the two files obvious.

3.  **Update `README.md`:**
    - A new "Configuration" section will be added to the `README.md`.
    - This section will explain the role of `.env` and `config.yaml`.
    - It will instruct the user to copy `.env.example` to `.env` and fill in the required values.
    - It will briefly explain how to enable different providers by adding their respective API keys.

# llm-middleware-telegram - Multi-Backend LLM Telegram Bot

This Telegram bot connects to various Large Language Model (LLM) backends like Ollama, Google Gemini, and OpenRouter, allowing users to interact with different AI models through a Telegram interface.

## Features

*   **Multiple LLM Backends:** Supports Ollama (local or remote), Google Gemini, OpenRouter, and any OpenAI-compatible API via configuration.
*   **Provider Switching:** Users can switch between all configured LLM providers using the `/provider` command.
*   **Generic Model Selection:** Users can list and select models for the *active* provider using `/list_models` and `/set_model`.
*   **Conversation Threads:** Maintains separate conversation histories for different chats or threads within a chat (`/new` command).
*   **Streaming Responses:** Edits messages in place to show responses as they are generated.
*   **Configuration:** Flexible configuration via `.env` for secrets and `config.yaml` for settings.
*   **Dockerized:** Easy deployment using Docker and Docker Compose.

## Prerequisites

*   **Python:** 3.10 or higher
*   **Docker:** Latest version recommended
*   **Docker Compose:** Latest version recommended
*   **API Keys/Tokens:**
    *   Telegram Bot Token
    *   Google Gemini API Key(s) (if using Gemini)
    *   OpenRouter API Key (if using OpenRouter)
    *   API Keys for any Custom OpenAI-compatible providers you configure.
*   **Ollama:** Running instance accessible from the Docker container (if using Ollama).

## Setup Instructions

1.  **Clone the repository:**
    ```bash
    git clone <repository_url> # Replace with the actual URL
    cd llm-middleware-telegram
    ```

2.  **Create the environment file:**
    *   Copy the example environment file:
        ```bash
        cp .env.example .env
        ```
    *   **Edit the `.env` file** and add your actual API keys and tokens:
        *   `TELEGRAM_BOT_TOKEN`: Your Telegram bot token from BotFather.
        *   `GEMINI_API_KEY_1` (and potentially others like `GEMINI_API_KEY_2`, etc., or `GEMINI_API_KEYS`): Your Google AI Studio API key(s).
        *   `OPENROUTER_API_KEY`: Your OpenRouter API key.
        *   `GROQ_API_KEY`, `ANOTHER_PROVIDER_API_KEY`, etc.: API keys for any custom providers you add in `config.yaml`. The environment variable name must match the provider name (uppercase) + `_API_KEY`.

3.  **Configure `config.yaml`:**
    *   Review the `config.yaml` file.
    *   Adjust default providers and models if desired.
    *   **Add Custom Providers:** Add entries under the `custom_openai_providers` list to configure providers with OpenAI-compatible APIs (like Groq, Requesty, Together AI, etc.). For each custom provider, specify:
        *   `name`: A unique identifier (e.g., "groq", "requesty").
        *   `base_url`: The API base URL (e.g., "https://api.groq.com/openai/v1", "https://router.requesty.ai/v1").
        *   `default_model`: The default model ID for this provider (e.g., "llama3-8b-8192", "openai/gpt-4o").
        *   `allowed_models` (Optional): A list of model IDs users can select with `/set_model`. If omitted, only the default model might be selectable via commands.
    *   Ensure you have set the corresponding API key in your `.env` file (e.g., `GROQ_API_KEY` for a provider named "groq").

4.  **Build and Run with Docker Compose:**
    ```bash
    docker compose up --build -d
    ```
    *   The `-d` flag runs the container in detached mode (in the background). Omit it if you want to see the logs directly in your terminal.
    *   To view logs when running detached: `docker compose logs -f`
    *   To stop the bot: `docker compose down`

## Usage

Interact with your bot in Telegram:

*   **/start:** Shows a welcome message.
*   **/help:** Displays available commands.
*   **/new:** Starts a new conversation thread (clears history for the current chat).
*   **/provider:** Shows the current LLM provider and allows switching between all configured providers (including custom ones).
*   **/model:** Shows the currently selected model for the active provider.
*   **/list_models:** Lists available/allowed models for the *currently active* provider. (For Ollama, this fetches dynamically; for others, it uses `allowed_models` from `config.yaml`).
*   **/set_model `<model_name>`:** Sets the model for the *currently active* provider. You can type the name or select from the buttons shown by `/list_models`.
*   **Any other text:** Sent as a prompt to the currently selected LLM provider and model.

## Configuration Details

*   **`.env`:** Stores all sensitive API keys and tokens. See `.env.example` for required variables.
*   **`config.yaml`:** Stores non-sensitive settings:
    *   `default_provider`: The LLM provider used if none is set for a chat (e.g., "ollama", "gemini", "groq").
    *   `default_ollama_model`, `default_gemini_model`, `default_openrouter_model`: Default models for built-in providers.
    *   `gemini_ask_all_models`: List of models used by the `/ask_all_gemini` command and selectable via `/list_models` when Gemini is active.
    *   `openrouter_allowed_models`: List of OpenRouter models selectable via `/list_models` when OpenRouter is active.
    *   `custom_openai_providers`: A list defining custom providers (see Setup section). Each item needs `name`, `base_url`, `default_model`, and optionally `allowed_models`.
    *   `session_file_path`: Location to store conversation history and user settings. Avoid JSON format for production deployments - use SQLite or database backend instead.
    *   `REQUEST_TIMEOUT_SECONDS`: Timeout for waiting for responses from LLM APIs.
    *   `default_max_context_tokens`: Default maximum tokens for history sent to LLMs and for storing history (helps prevent exceeding model limits)
    *   `allowed_chat_ids` (Optional): Uncomment and list specific Telegram chat IDs to restrict bot usage. If commented out or empty, the bot responds in any chat it's added to.

## Known Issues and Troubleshooting

### 1. Session Storage Scalability
*   **Cause:** JSON file storage becomes inefficient with large conversation histories
*   **Impact:** Slower response times as session data grows
*   **Mitigation:**
    *   Atomic saves to JSON are implemented, but a database backend is recommended for true scalability

### 2. Redundant Handlers
*   **Cause:** Multiple command handlers performing similar validation checks
*   **Impact:** Code duplication and maintenance overhead
*   **Mitigation:**
    *   Create base handler class with common validation logic
    *   Refactor provider-specific handlers to inherit from base

### 3. Telegram Network Errors (`Bad Gateway`, `NetworkError`)

*   **Cause:** Transient connectivity issues with Telegram servers or API downtime.
*   **Impact:** Bot may temporarily stop receiving updates or sending messages.
*   **Mitigation:**
    *   The bot includes retry logic, but persistent issues may require manual restart.
    *   Check your internet connection and Telegram status.
    *   Consider adding exponential backoff and alerting for repeated failures.

### 4. HTTP Connection Failures (`httpx.ConnectError`)

*   **Cause:** Network problems reaching LLM APIs or Telegram.
*   **Impact:** API calls fail, leading to incomplete responses.
*   **Mitigation:**
    *   Ensure API endpoints are reachable.
    *   Check firewall/proxy settings.
    *   Implement retries with backoff in service layer (`services/`).

### 5. Message Edit Errors (`Message is not modified`)

*   **Cause:** Bot attempts to edit a message with identical content.
*   **Impact:** Benign error, but clutters logs.
*   **Mitigation:**
    *   Catch and ignore this specific error.
    *   Or, compare content before editing.

### 6. Telegram Flood Control

*   **Cause:** Too many messages or edits in a short time.
*   **Impact:** Delays in message delivery.
*   **Mitigation:**
    *   Throttle message sending.
    *   Avoid unnecessary edits.
    *   Respect Telegram rate limits.

### 7. LLM Context Management
*   **Cause:** Uses a global token limit for history truncation
*   **Impact:** May not account for model-specific context window sizes
*   **Mitigation:**
    *   Uses a globally configurable token limit (`default_max_context_tokens`)
    *   Future enhancement: model-specific context window management

## Additional Reminders

*   **Project Structure:**  
    Place new error handling or retry logic in the appropriate `services/` or `bot/handlers/` files.

*   **Provider Abstraction:**  
    Wrap API calls with try/except, log errors, and provide user-friendly fallback messages.

*   **Configuration:**  
    If adding retry/backoff settings, put them in `config.yaml` or `.env` as appropriate.

*   **Session Management:**  
    Avoid excessive writes during retries or error states.

*   **Command Handling:**  
    Catch and handle user input errors gracefully.

*   **Error Handling:**  
    Log all exceptions clearly, but avoid crashing the bot on transient issues.

*   **Documentation:**  
    Update this README section if new error patterns emerge or fixes are implemented.

## To-Do List

1.  **Input Validation:** Add schema validation for custom provider configurations in config.yaml
2.  **Handler Refactoring:** Consolidate common command validation logic into base classes
3.  **Session Encryption:** Implement secure storage for sensitive session data
4.  **Documentation Updates:** Expand configuration examples for custom providers and troubleshooting guides.
5.  **Model-Specific Context:** Implement per-model context window size management

## Contributing

Contributions are welcome! If you'd like to help with any of the to-do items or suggest improvements, please open an issue or submit a pull request.

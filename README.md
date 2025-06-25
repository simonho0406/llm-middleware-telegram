# llm-middleware-telegram - Multi-Backend LLM Bot

This Telegram bot connects to various Large Language Model (LLM) backends like Ollama, Google Gemini, and OpenRouter, allowing users to interact with different AI models through a Telegram interface.

## Features

*   **Multiple LLM Backends:** Supports Ollama (local or remote), Google Gemini, OpenRouter, and any OpenAI-compatible API via configuration.
*   **Provider & Model Switching:** Dynamically switch between providers (`/provider`) and select from paginated model lists (`/list_models`, `/set_model`).
*   **Conversation Threads:** Maintains separate, persistent conversation histories that can be created (`/new`), listed (`/threads`), and renamed (`/rename_thread`).
*   **Streaming Responses:** Edits messages in place to show responses as they are generated.
*   **Advanced Multi-Model Tools:**
    *   **/ask_selected:** Query multiple models concurrently with a single prompt.
    *   **/discuss:** A "Round Table" feature where multiple models from a single provider engage in a sequential, turn-by-turn conversation to refine an answer.
*   **Web Search:** Provides real-time information to the LLM to answer questions about current events using the `/search` command.
*   **Dockerized:** Easy deployment using Docker and Docker Compose.

## Prerequisites

*   **Python:** 3.11 or higher
*   **Docker & Docker Compose:** Latest versions recommended
*   **API Keys/Tokens:**
    *   A valid Telegram Bot Token.
    *   API keys for any providers you wish to use (Gemini, OpenRouter, Groq, etc.).
*   **Ollama:** A running instance if you intend to use the Ollama provider.

## Setup Instructions

1.  **Clone the repository:**
    ```bash
    git clone <repository_url>
    cd llm-middleware-telegram
    ```

2.  **Create and configure the environment file:**
    ```bash
    cp .env.example .env
    ```
    Edit the `.env` file and add your actual API keys and tokens. See `.env.example` for the required format.

3.  **Configure `config.yaml`:**
    *   Review `config.yaml` to set default providers, models, and custom provider endpoints.
    *   For custom OpenAI-compatible providers, ensure you define the `name`, `base_url`, and `default_model`. The corresponding API key must be set in `.env` (e.g., a provider named "groq" requires `GROQ_API_KEY` in the `.env` file).

4.  **Build and Run with Docker Compose:**
    ```bash
    docker compose up --build -d
    ```
    *   Use `docker compose logs -f` to view logs.
    *   Use `docker compose down` to stop the bot.

## Usage

*   **/help:** Displays the main list of commands.
*   **/new:** Starts a new, empty conversation thread.
*   **/reroll:** Regenerates the last AI response.
*   **/provider:** Switch between configured AI providers.
*   **/model:** Show and set the model for the current provider.
*   **/discuss <prompt>:** Starts a sequential, multi-model discussion on a topic.
*   **/search <query>:** Answers a query using live web search results.
*   **/threads:** List, switch between, or delete your conversation threads.

## Project Roadmap & Priorities

Development is guided by a strategic roadmap focused on evolving the bot into a sophisticated collaborative AI tool.

### Phase 1: Multi-Agent "Round Table" Discussion
*   **Status:** ✅ **Completed & Stabilized**
*   **Description:** The `/discuss` command allows a user to select multiple models from a single provider to engage in a sequential conversation, where each model critiques or builds upon the previous one's response.

### Phase 1.5: Multi-Provider Discussion (Next)
*   **Status:** 📝 **Planned**
*   **Description:** Evolve the `/discuss` command to allow selecting models from *different* providers. This will enable more powerful, heterogeneous agent chains (e.g., a fast model for outlining, a powerful model for generation) and mitigate single-provider rate limits.

### Phase 2: Multi-Agent "Expert Panel" (Future)
*   **Status:** 📝 **Planned**
*   **Description:** A true multi-agent system where a lead agent decomposes a query into sub-tasks, assigns them to specialized sub-agents that execute in parallel, and a final agent synthesizes the results.

### High-Priority Technical & Feature Work
1.  **Database Migration:** Migrate session storage from `sessions.json` to a scalable database backend (e.g., SQLite) to ensure performance and data integrity. This is a prerequisite for more advanced agentic features.
2.  **Seamless Tool Integration (Automatic Search):** Evolve the bot to automatically detect when a user's query requires up-to-date information, triggering the `/search` workflow without manual user intervention.

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
    *   Uses sessions.json with atomic saves. Migration to a database backend (e.g., SQLite) is the top priority for true scalability and performance.

### 2. Telegram Network Errors (`Bad Gateway`, `NetworkError`)

*   **Cause:** Transient connectivity issues with Telegram servers or API downtime.
*   **Impact:** Bot may temporarily stop receiving updates or sending messages.
*   **Mitigation:**
    *   The bot includes retry logic, but persistent issues may require manual restart.
    *   Check your internet connection and Telegram status.
    *   Consider adding exponential backoff and alerting for repeated failures.

### 3. HTTP Connection Failures (`httpx.ConnectError`)

*   **Cause:** Network problems reaching LLM APIs or Telegram.
*   **Impact:** API calls fail, leading to incomplete responses.
*   **Mitigation:**
    *   Ensure API endpoints are reachable.
    *   Check firewall/proxy settings.
    *   Implement retries with backoff in service layer (`services/`).

### 4. Message Edit Errors (`Message is not modified`)

*   **Cause:** Bot attempts to edit a message with identical content.
*   **Impact:** Benign error, but clutters logs.
*   **Mitigation:**
    *   Catch and ignore this specific error.
    *   Or, compare content before editing.

### 5. Telegram Flood Control

*   **Cause:** Too many messages or edits in a short time.
*   **Impact:** Delays in message delivery.
*   **Mitigation:**
    *   Throttle message sending.
    *   Avoid unnecessary edits.
    *   Respect Telegram rate limits.

### 6. Gemini Context Recall Issues
*   **Cause:** When using the Gemini provider, the LLM may occasionally not recall earlier parts of the current conversation thread, particularly after a provider switch or if unrelated API errors occurred previously in the thread.
*   **Impact:** Inconsistent conversation history for Gemini
*   **Mitigation:**
    *   This is under active investigation

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

## Contributing

Contributions are welcome! If you'd like to help with any of the roadmap items or suggest improvements, please open an issue or submit a pull request.

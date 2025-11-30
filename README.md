# llm-middleware-telegram - Multi-Backend LLM Bot

This Telegram bot connects to various Large Language Model (LLM) backends like Ollama, Google Gemini, and OpenRouter, allowing users to interact with different AI models through a Telegram interface.

## Features

*   **Multiple LLM Backends:** Supports Ollama (local or remote), Google Gemini, OpenRouter, and any OpenAI-compatible API via configuration.
*   **Provider & Model Switching:** Dynamically switch between providers (`/provider`) and select from paginated model lists (`/list_models`, `/set_model`).
*   **Conversation Threads:** Maintains separate, persistent conversation histories that can be created (`/new`), listed (`/threads`), and renamed (`/rename_thread`).
*   **Streaming Responses:** Edits messages in place to show responses as they are generated.
*   **Advanced Multi-Model Tools:**
    *   **/ask_selected:** Query multiple models concurrently with a single prompt.
    *   **/discuss:** A "Round Table" feature where multiple models from any configured provider engage in a sequential, turn-by-turn conversation to refine an answer.
*   **Web Search:** Provides real-time information to the LLM to answer questions about current events using the `/search` command.
*   **Dockerized:** Easy deployment using Docker and Docker Compose.

## Prerequisites

*   **Python:** 3.11 or higher
*   **Docker & Docker Compose:** Latest versions recommended
*   **API Keys/Tokens:**
    *   A valid Telegram Bot Token.
    *   API keys for any providers you wish to use (Gemini, OpenRouter, Groq, etc.).
*   **Ollama:** A running instance if you intend to use the Ollama provider.

## Configuration

Setting up the bot involves two main files: `.env` for your secret keys and `config.yaml` for public settings.

### 1. Environment Variables (`.env`)

This file stores all your secret API keys and tokens. It should never be committed to version control.

1.  **Create the file:** Copy the example file to a new file named `.env`:
    ```bash
    cp .env.example .env
    ```
2.  **Edit the file:** Open `.env` and fill in the required values.
    *   `TELEGRAM_BOT_TOKEN` is **required**.
    *   Fill in the API keys for any providers you want to use (e.g., `GEMINI_API_KEYS`, `OPENROUTER_API_KEY`). If you don't add a key for a provider, you won't be able to use its models.

### 2. YAML Configuration (`config.yaml`)

This file controls the bot's behavior, default models, and provider definitions. You can edit it to:
*   Set the `default_provider` and default models for each service.
*   Define custom OpenAI-compatible providers. The `api_key` field for a custom provider must match the name of the environment variable you created in your `.env` file.
*   Configure the Expert Panel agents and quality thresholds.

> **A Note for Docker Users:** If you are running services like Ollama on your host machine (outside of Docker), you cannot use `localhost` in your `.env` file to connect to them from the bot's container. You must use the special DNS name `host.docker.internal`. For example: `OLLAMA_HOST=http://host.docker.internal:11434`.

## Setup Instructions

1.  **Clone the repository:**
    ```bash
    git clone <repository_url>
    cd llm-middleware-telegram
    ```

2.  **Configure the bot** by following the steps in the "Configuration" section above.

3.  **Build and Run with Docker Compose:**
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
*   **/search <query>:** Answers a query using live web search results. Includes a "Retry" button for failed searches.
*   **/threads:** List, switch between, or delete your conversation threads.
*   **/ask_selected <prompt>:** Query multiple models concurrently with a single prompt.

### Panel Discussion Features
*   **Refinement via Edit:** You can edit your follow-up messages during a panel discussion to correct mistakes or change direction. The bot will automatically cancel the current generation and restart the round with your updated input.
*   **Cancellation:** Use `/cancel` at any time to stop a panel discussion immediately.

## Project Roadmap & Priorities

Development is guided by a strategic roadmap focused on evolving the bot into a sophisticated collaborative AI tool.

### Phase 1: Multi-Agent "Round Table" Discussion
*   **Status:** ✅ **Completed & Stabilized**
*   **Description:** The `/discuss` command allows a user to select multiple models from a single provider to engage in a sequential conversation, where each model critiques or builds upon the previous one's response.

### Phase 1.5: Multi-Provider Discussion
*   **Status:** ✅ **Completed & Stabilized**
*   **Description:** Evolve the `/discuss` command to allow selecting models from *different* providers. This will enable more powerful, heterogeneous agent chains (e.g., a fast model for outlining, a powerful model for generation) and mitigate single-provider rate limits.

### Phase 2: Multi-Agent "Agentic Workshop"
*   **Status:** ✅ **Completed & V2.0 Released**
*   **Description:** A true multi-agent system where a lead "Orchestrator" agent decomposes a query, assigns tasks to specialized sub-agents (e.g., "Proposer", "Critic"), and then decides if the quality is sufficient or if another iteration of refinement is needed. This "Quality Gate" loop allows the system to dynamically improve its response before synthesizing a final answer.

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
    *   The bot includes robust retry logic that automatically restarts the polling loop and recreates the application connection.
    *   Check your internet connection and Telegram status.

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

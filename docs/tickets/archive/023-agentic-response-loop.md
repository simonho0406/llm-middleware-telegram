# Ticket 023: Agentic Response Loop & Provider Integration

**Priority:** P0
**Component:** Bot Core Pipeline / Services
**Status:** ✅ Implemented & Verified
**Prerequisites:** Ticket 020, Ticket 021, Ticket 022

---

## 1. Description
Integrate the multi-turn recursive agentic tool-use loop into `response_generator.py` and upgrade the LLM provider services to translate and handle tools and tool calling payloads natively.

## 2. Architectural Pillars (Immutable)
*   **Pillar B (Centralized, Safe Rendering)**: Visually communicate intermediate execution status (e.g. `[🔧 Running Tavily Search...]`) using safe draft-sending mechanisms. Sanitized tool responses are never rendered directly to the user as final text; only the LLM's final synthesized response is rendered via `send_safe_message`.
*   **Pillar C (Robust State Management)**: Implement recursive depth boundaries (maximum 5 tool turns) to prevent runaway infinite billing or rate-limit loops.

## 3. Proposed Changes

### 3.1 Adapt LLM Services for Tool-Calling
#### Modify [services/openai_compatible_service.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/services/openai_compatible_service.py)
*   Modify `generate_response()` signature:
    ```python
    async def generate_response(self, model: str, prompt: str, context_history: list = None, request_timeout: int = None, tools: list = None):
    ```
*   When compiling history for strict APIs (like NVIDIA), map standard tool exchange structures:
    -   Assistant messages with `tool_calls` must map to `tool_calls` payload in `messages`.
    -   Tool messages with `tool_call_id` and `content` must be passed with role `tool`.
*   Pass `tools` parameter directly into `client.chat.completions.create` if provided.
*   In streaming and non-streaming loops, check if the completion delta contains `tool_calls`. If it does, aggregate and yield a structured JSON tool call indicator (e.g. `{"tool_calls": [...]}`) instead of plain text content.

#### Modify [services/gemini_service.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/services/gemini_service.py)
*   Modify `generate_response()` signature to support the `tools` parameter.
*   Translate OpenAI-formatted tools schema list to Google GenAI v2 SDK `types.Tool` structures (specifically `types.FunctionDeclaration` inside `types.Tool`).
*   Map OpenAI-compatible tool calling histories:
    -   Translate history items containing `tool_calls` and `tool_call_id` to Gemini's native `FunctionCall` and `FunctionResponse` structures inside `parts` arrays of the conversation messages.
*   Yield structured JSON containing `{"tool_calls": [...]}` if the stream delta yields a function call request.

#### Modify [bot/settings.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/settings.py)
*   Define boolean settings `enable_mcp` (default True) and `enable_skills` (default True) so the `/config` UI registers them.

---

### 3.2 Implement the Agentic Loop
#### Modify [bot/response_generator.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/bot/response_generator.py)
*   Upgrade `_generate_llm_response()` to wrap the response generation in a loop:
    ```python
    MAX_TOOL_TURNS = 5
    for turn in range(MAX_TOOL_TURNS):
        # 1. Fetch enable_mcp and enable_skills user settings.
        #    If enabled, fetch all tools from McpClientService and/or SkillRegistryService.
        # 2. Pass tools to generate_response()
        # 3. Stream output
        # 4. If output is a tool call request:
        #    a. Send dynamic progress message to Telegram (e.g. "Executing mysql_query...")
        #    b. Validate tool call with hook_runner
        #    c. Execute tool (McpClientService.execute_tool or skill playbook runner)
        #    d. Save the assistant tool_call message & the system tool_result message to DB
        #    e. Continue loop with updated history
        # 5. If output is standard text:
        #    a. Return final synthesized answer (exit loop)
    ```

## 4. Verification & Testing
*   **Test Case 1 (Recursive Execution)**: Mock the LLM service to return a tool call requesting search on the first turn, and return final text on the second turn. Assert the orchestrator handles the recursion, calls the search tool, updates history, and returns the final synthesized response correctly.
*   **Test Case 2 (Provider Translation)**: Verify that `gemini_service.py` maps OpenAI function calling structures to Google's SDK structure without throwing validation errors.
*   **Test Case 3 (Infinite Loop Guard)**: Force a mock tool call that always triggers another tool call. Assert that after `MAX_TOOL_TURNS = 5`, the loop terminates cleanly and returns a standardized warning message to the user.

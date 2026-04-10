# Ticket 007: Model Context Protocol (MCP) Integration Specification

**Priority:** P2
**Phase:** Phase 4 (New Feature Integration)
**Status:** Defined / Ready for Implementation

## 1. Executive Summary

The middleware currently hardcodes specialized logic, such as Web Search triggers, via regex string manipulation (e.g., `<search>` tags). To achieve the goal of a true **Frontend-Agnostic LLM Middleware**, all tools and external APIs must be abstracted and dynamically loaded via the **Model Context Protocol (MCP)**. 

This specification outlines the integration of an MCP Client subsystem that dynamically mounts tools from user-configured servers, translates them into LLM-native formats, executes them upon LLM request, and safely injects the results back into the conversation context.

## 2. Architectural Pillars (Immutable)

1. **Protocol Compliance**: Use the official `mcp` Python SDK (Anthropic) for lifecycle management to avoid brittle, custom-rolled JSON-RPC over `stdio`.
2. **Stateless Service Pattern**: The `McpClientService` must be a class instantiated per-request or held in a thread-safe connection pool, avoiding global mutable module state.
3. **Provider-Agnostic Tooling**: Tool schemas retrieved from MCP must be transformed into the standard OpenAI Tool Calling format, which is the baseline schema we will enforce across OpenRouter, Gemini, and Local Ollie.
4. **Resilient UI Rendering**: Tool execution must be visually communicated to the User via `send_draft_message` and sanitized via `send_safe_message`.

---

## 3. Implementation Blueprint

### 3.1 Dependencies
Update `requirements.txt` to include the official MCP python SDK:
```text
mcp
```

### 3.2 Configuration Map (`config.yaml`)
Create a new section in `config.yaml` to define MCP servers:
```yaml
mcp_servers:
  - name: "sqlite-tools"
    command: "uvx"
    args: ["mcp-server-sqlite", "--db-path", "data/app.db"]
    transport: "stdio"
```

### 3.3 The `McpClientService` (`services/mcp_service.py`)
Create a class responsible for managing connections and routing:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class McpClientService:
    def __init__(self, config_block: dict):
         self.server_configs = config_block
         self.sessions = {} # Dictionary mapping server_name to Active ClientSession
         
    async def connect_all(self):
         """Iterate config, spawn stdio subprocesses, initialize sessions."""
         pass
         
    async def get_all_tools(self) -> list[dict]:
         """Merge all tool schemas from across all connected MCP servers and format them to OpenAI schema."""
         pass
         
    async def execute_tool(self, server_name: str, tool_name: str, arguments: dict):
         """Route the tool invocation to the correct server session and return result string."""
         pass
```

### 3.4 Database & Storage Adjustments
The `messages` table must be expanded to support native tool interaction blocks.
- **Role adjustments**: Support `tool_call` (from assistant) and `tool_result` (appended system context).
- **History Retrieval**: `storage_manager.py` must serialize tool exchanges cleanly back into the provider's specific API format during `get_thread_history()`.

### 3.5 Orchestrator Pipeline (`bot/response_generator.py`)

The core loop in `_generate_llm_response` must be upgraded to a multi-turn recursive loop:
1. Fetch `tools` from `McpClientService.get_all_tools()`.
2. Inject `tools` parameter into `service.generate_response`.
3. **If the LLM yields a text stream**: Stream it to Telegram as usual using `send_draft_message`.
4. **If the LLM yields a `tool_call`**:
   - Send UI Draft: `[Agent is executing tool: {tool_name}...]`
   - Store the assistant's `tool_call` securely into DB.
   - Run `McpClientService.execute_tool()`.
   - Store the outcome in DB under role `tool_result`.
   - Recurse back to Step 1 with the updated, larger history, allowing the LLM to synthesize the data.

### 3.6 Provider API Harmonization
Currently, `OpenAICompatibleService`, `GeminiService`, and `OllamaService` share a `generate_response()` signature.
This signature must be expanded:
```python
async def generate_response(
    self, 
    model: str, 
    prompt: str, 
    context_history: list, 
    tools: list = None
):
```
Each class will be responsible for translating the generic OpenAI `tools` dict into its native provider syntax (e.g., Gemini's `types.Tool` objects).

---

## 4. Rollout Strategy & Verification

- **Phase A**: Scaffolding. Build `McpClientService`, update DB schemas. (Test: Can we ping a local sqlite MCP server and dump the schema locally?)
- **Phase B**: Provider bridging. Adapt `OpenAICompatibleService` first, handling the recursive tool-call logic loop.
- **Phase C**: Telegram UI bridge. Add dynamic UI draft messages to inform users of unseen execution (e.g., search fetching).

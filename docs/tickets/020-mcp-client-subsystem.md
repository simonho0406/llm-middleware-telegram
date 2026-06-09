# Ticket 020: Model Context Protocol (MCP) Client Subsystem

**Priority:** P1
**Component:** Services / External Integration
**Status:** ✅ Implemented & Verified
**Prerequisites:** None

---

## 1. Description
Implement a production-grade MCP Client utilizing the official Anthropic `mcp` SDK. This subsystem will dynamically connect to local stdio-based MCP servers, query their available tool schemas, and execute tools on behalf of the LLM orchestrator.

## 2. Architectural Pillars (Immutable)
*   **Pillar A (Stateless Service)**: The `McpClientService` must be an instantiable class without global module-level connection variables. It will be instantiated or retrieved from a thread-safe container.
*   **Pillar C (Robust State Management)**: Spawned stdio subprocesses must be tracked in an internal active process registry and cleanly terminated (`SIGTERM`/`SIGKILL`) on shutdown, timeouts, or task cancellation to prevent zombie process leaks.

## 3. Proposed Changes

### 3.1 Setup Dependencies
Add the following to [requirements.txt](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/requirements.txt):
```text
mcp
```

### 3.2 Add Configuration Block
Update [config.yaml](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/config.yaml) to declare MCP servers:
```yaml
mcp_servers:
  - name: "sqlite-tools"
    command: "uvx"
    args: ["mcp-server-sqlite", "--db-path", "data/app.db"]
    transport: "stdio"
```

### 3.3 Create the McpClientService Class
Create [services/mcp_service.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/services/mcp_service.py):
*   Class signature:
    ```python
    from typing import Dict, List, Any
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    class McpClientService:
        def __init__(self, server_configs: List[Dict[str, Any]]):
            self.server_configs = server_configs
            self.sessions: Dict[str, ClientSession] = {}
            self.exit_stack = None  # To manage context stacks of stdio clients
            
        async def connect_all(self):
            """Iterates configs, starts stdio clients, initializes sessions, runs startup handshake."""
            pass

        async def get_all_tools(self) -> List[Dict[str, Any]]:
            """Lists all tools from all connected servers, standardizing their schemas to OpenAI Tool format."""
            pass

        async def execute_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> str:
            """Routes tool invocation to the correct server session and returns the string outcome."""
            pass

        async def cleanup_all(self):
            """Cleanly closes sessions and terminates child subprocesses."""
            pass
    ```

## 4. Verification & Testing
*   **Test Case 1 (Mock Stdio)**: Write a pytest using a dummy Python script that reads stdin/writes stdout as a mock stdio server. Assert `McpClientService` connects, retrieves tools list, executes a tool, and successfully terminates the subprocess without leaving a zombie.
*   **Test Case 2 (Config Loading)**: Verify that when `config.yaml` is parsed, the list of configured servers translates correctly to the initialization parameters of `McpClientService`.

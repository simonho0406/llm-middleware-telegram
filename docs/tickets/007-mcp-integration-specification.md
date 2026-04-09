# Ticket 007: Model Context Protocol (MCP) Integration Specification

## Problem
The middleware's tool capabilities (search, file operations) are currently hardcoded in Python. To be a true Frontend-Agnostic LLM Middleware, it must support dynamic tool exposure via the Model Context Protocol (MCP).

## Architecture Guidelines (Immutable)
- **Class-Based Service**: The MCP Client must be implemented as a stateful, class-based service (e.g. `McpClientService`) instantiated with config.
- **Protocol Extensibility**: Initially, support `stdio` servers for local tool execution, followed by `http/sse`.

## Required Changes
1. **`services/mcp_service.py`**
   - Create a service that parses a `mcp_servers.yaml` configuration file.
   - Implement lifecycle management (start process, stop process, send JSON-RPC over `stdio`).
   - Implement `list_tools()` to retrieve tools from the external MCP server.
   - Implement `call_tool(name, arguments)` to route LLM sub-queries to the MCP server.

2. **Provider Orchestration (`bot/response_generator.py`)**
   - Map the external MCP tools returned by `list_tools()` into the native OpenAI-compatible tool JSON schema format for the LLM.
   - When the LLM calls an MCP tool, route it automatically to the `McpClientService`.

## Verification
- Start a mock `stdio` MCP server (e.g., using `uvx mcp-server-sqlite`) and verify the middleware can expose its tables and run queries via the LLM.

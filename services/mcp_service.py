import logging
import asyncio
import os
from typing import Dict, List, Any
import contextlib

logger = logging.getLogger(__name__)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    # Handle the case where mcp is not installed for early failure or testing without the library
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

_active_mcp_service = None

def get_active_mcp_service():
    """Retrieve the globally active McpClientService instance."""
    global _active_mcp_service
    return _active_mcp_service

class McpClientService:
    def __init__(self, server_configs: List[Dict[str, Any]]):
        self.server_configs = server_configs
        self.sessions: Dict[str, "ClientSession"] = {}
        self.exit_stack = contextlib.AsyncExitStack()
        
    async def connect_all(self):
        """Iterates configs, starts stdio clients, initializes sessions, runs startup handshake."""
        global _active_mcp_service
        _active_mcp_service = self
        if not stdio_client:
            logger.error("mcp SDK is not installed. Cannot start MCP Client Subsystem.")
            return

        for config in self.server_configs:
            name = config.get("name")
            command = config.get("command")
            args = config.get("args", [])
            transport = config.get("transport", "stdio")

            if transport != "stdio":
                logger.warning(f"Transport '{transport}' for MCP server '{name}' is not currently supported. Only 'stdio' is supported.")
                continue

            try:
                # Build a minimal environment for the subprocess: only pass keys that this
                # specific MCP server needs (declared via pass_env in config.yaml) plus the
                # base vars required by the OS/runtime. This prevents secret leakage to a
                # compromised MCP server (e.g. a malicious npx package update).
                _base_env_keys = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
                                  "NODE_PATH", "UV_PYTHON", "PYTHONPATH"}
                _pass_env_keys = set(config.get("pass_env", []))
                env = {k: v for k, v in os.environ.items()
                       if k in _base_env_keys or k in _pass_env_keys}
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env
                )
                
                # Setup context managers via exit stack to ensure cleanup
                stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
                read, write = stdio_transport
                
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                
                await session.initialize()
                
                self.sessions[name] = session
                logger.info(f"Connected to MCP server '{name}' successfully.")
            except Exception as e:
                # Use error (not exception) — the MCP SDK traceback is not actionable;
                # the error message alone tells us what failed.
                logger.error(f"Failed to connect to MCP server '{name}': {e}")

    async def get_all_tools(self) -> List[Dict[str, Any]]:
        """Lists all tools from all connected servers, standardizing their schemas to OpenAI Tool format."""
        all_tools = []
        for server_name, session in self.sessions.items():
            try:
                response = await session.list_tools()
                for tool in response.tools:
                    # Translate to OpenAI function format and namespace the tool name
                    open_ai_tool = {
                        "type": "function",
                        "function": {
                            "name": f"{server_name}__{tool.name}",
                            "description": tool.description,
                            "parameters": tool.inputSchema
                        }
                    }
                    all_tools.append(open_ai_tool)
            except Exception as e:
                logger.error(f"Failed to list tools for MCP server '{server_name}': {e}")
        return all_tools

    async def execute_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Routes tool invocation to the correct server session and returns the string outcome."""
        session = self.sessions.get(server_name)
        if not session:
            return f"[Error: MCP server '{server_name}' is not connected or does not exist.]"

        try:
            result = await session.call_tool(tool_name, arguments=arguments)
            
            if result.isError:
                error_texts = [content.text for content in result.content if content.type == "text"]
                return f"[Error: Tool execution failed: {' | '.join(error_texts)}]"

            # Extract the text content from the successful response
            texts = [content.text for content in result.content if content.type == "text"]
            if not texts:
                return "[Success: Tool executed but returned no text.]"
            return "\n".join(texts)
            
        except Exception as e:
            logger.exception(f"Exception executing tool '{tool_name}' on server '{server_name}': {e}")
            return f"[Error: Exception during tool execution: {str(e)}]"

    async def cleanup_all(self):
        """Cleanly closes sessions and terminates child subprocesses."""
        global _active_mcp_service
        if _active_mcp_service is self:
            _active_mcp_service = None
        try:
            await self.exit_stack.aclose()
            self.sessions.clear()
            logger.info("Cleaned up all MCP sessions and subprocesses.")
        except Exception as e:
            logger.exception(f"Error during MCP cleanup: {e}")

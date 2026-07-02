import logging
import asyncio
import os
from typing import Dict, List, Any
import contextlib
from config import get_env  # imported by name: the connect_all loop var `config` shadows the module

logger = logging.getLogger(__name__)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    # Handle the case where mcp is not installed for early failure or testing without the library
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

class McpClientService:
    def __init__(self, server_configs: List[Dict[str, Any]]):
        self.server_configs = server_configs
        self.sessions: Dict[str, "ClientSession"] = {}
        self.exit_stack = contextlib.AsyncExitStack()
        # Per-server tool allowlist (bare tool names). None => no restriction (expose all).
        #
        # This is a POSITIVE, default-deny allowlist enforced at TWO points:
        #   - get_all_tools(): non-allowlisted tools are never shown to the model, so they
        #     aren't in its vocabulary and don't cost context tokens.
        #   - execute_tool(): a call to a non-allowlisted tool is rejected in code, so even
        #     a stale tool reference or an INJECTED tool_call (indirect prompt injection via
        #     web/DB/Notion content) cannot invoke something off-list.
        # Motivation: the prior gate was a 3-item denylist (sqlite writes only); every other
        # tool — notably tavily's arbitrary-URL fetchers (crawl/extract/map/research), a
        # data-exfiltration channel — was reachable. An allowlist inverts the default to deny.
        self._allowed_tools: Dict[str, Any] = {}
        for cfg in server_configs:
            name = cfg.get("name")
            allow = cfg.get("allowed_tools")
            self._allowed_tools[name] = set(allow) if allow is not None else None

    async def connect_all(self):
        """Iterates configs, starts stdio clients, initializes sessions, runs startup handshake."""
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
                # Route through config.get_env so forwarded secrets (e.g. NOTION_TOKEN,
                # TAVILY_API_KEY in pass_env) get the same quote/whitespace stripping the bot
                # applies to its own keys — otherwise a quoted .env would corrupt the MCP
                # server's auth even though the bot's own auth works.
                env = {}
                for _k in (_base_env_keys | _pass_env_keys):
                    if _k in os.environ:
                        env[_k] = get_env(_k)
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
            allowed = self._allowed_tools.get(server_name)
            try:
                response = await session.list_tools()
                for tool in response.tools:
                    # Skip tools not on this server's allowlist (if one is configured), so
                    # the model never sees them.
                    if allowed is not None and tool.name not in allowed:
                        continue
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

        # Fail-closed allowlist enforcement: reject any tool not on the server's allowlist,
        # even if it was somehow requested (stale reference, or an injected tool_call from
        # untrusted tool output). This is the code-level backstop behind get_all_tools's
        # filtering — the security boundary does not depend on the model's cooperation.
        allowed = self._allowed_tools.get(server_name)
        if allowed is not None and tool_name not in allowed:
            logger.warning(
                f"Denied MCP tool '{server_name}__{tool_name}': not in allowed_tools for server "
                f"'{server_name}' (allowed: {sorted(allowed)})."
            )
            return f"[Error: Tool '{tool_name}' is not permitted on server '{server_name}'.]"

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
        """Cleanly closes sessions and terminates child subprocesses.

        Must be called from the SAME asyncio task that called connect_all() —
        anyio cancel scopes inside stdio_client require same-task entry/exit.
        See utils/service_registry.py for the supervisor pattern that enforces
        this in production.
        """
        try:
            await self.exit_stack.aclose()
            self.sessions.clear()
            logger.info("Cleaned up all MCP sessions and subprocesses.")
        except Exception as e:
            logger.exception(f"Error during MCP cleanup: {e}")

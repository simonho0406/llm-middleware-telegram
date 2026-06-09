import pytest
import sys
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

# Mock the mcp library if it is not installed so tests can still load
try:
    import mcp
except ImportError:
    mcp_mock = MagicMock()
    mcp_mock.ClientSession = MagicMock
    mcp_mock.client = MagicMock()
    sys.modules['mcp'] = mcp_mock
    sys.modules['mcp.client'] = MagicMock()
    sys.modules['mcp.client.stdio'] = MagicMock()

# Now we can import the service
from services.mcp_service import McpClientService

@pytest.fixture
def sample_config():
    return [
        {
            "name": "sqlite-tools",
            "command": "uvx",
            "args": ["mcp-server-sqlite", "--db-path", "data/app.db"],
            "transport": "stdio"
        }
    ]

@pytest.mark.asyncio
async def test_mcp_client_initialization_and_config(sample_config):
    """Test that McpClientService parses configs correctly."""
    service = McpClientService(server_configs=sample_config)
    assert len(service.server_configs) == 1
    assert service.server_configs[0]["name"] == "sqlite-tools"
    assert service.sessions == {}

@pytest.mark.asyncio
async def test_mcp_client_connect_all(sample_config):
    """Test connecting to stdio clients and storing sessions."""
    service = McpClientService(server_configs=sample_config)
    
    # We mock stdio_client and ClientSession.
    # stdio_client is an async context manager yielding (read_stream, write_stream)
    # ClientSession is an async context manager yielding a session.
    
    mock_read_stream = AsyncMock()
    mock_write_stream = AsyncMock()
    mock_session = AsyncMock()
    
    class MockStdioClientContextManager:
        async def __aenter__(self):
            return mock_read_stream, mock_write_stream
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockSessionContextManager:
        def __init__(self, *args, **kwargs):
            self.session = mock_session
        async def __aenter__(self):
            return self.session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch('services.mcp_service.stdio_client', return_value=MockStdioClientContextManager()), \
         patch('services.mcp_service.ClientSession', MockSessionContextManager):
         
         await service.connect_all()
         
         assert "sqlite-tools" in service.sessions
         assert service.sessions["sqlite-tools"] == mock_session
         # Verify that the session was initialized
         mock_session.initialize.assert_awaited_once()

@pytest.mark.asyncio
async def test_mcp_client_get_all_tools(sample_config):
    """Test fetching tool schemas from all connected servers."""
    service = McpClientService(server_configs=sample_config)
    
    mock_session = AsyncMock()
    # Mock list_tools() response
    mock_tools_response = MagicMock()
    mock_tool = MagicMock()
    mock_tool.name = "query_db"
    mock_tool.description = "Queries the database"
    mock_tool.inputSchema = {"type": "object", "properties": {"query": {"type": "string"}}}
    mock_tools_response.tools = [mock_tool]
    
    mock_session.list_tools.return_value = mock_tools_response
    service.sessions["sqlite-tools"] = mock_session
    
    tools = await service.get_all_tools()
    
    assert len(tools) == 1
    # Check that it translated into OpenAI format
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "sqlite-tools__query_db" # Namespaced
    assert tools[0]["function"]["description"] == "Queries the database"
    assert "query" in tools[0]["function"]["parameters"]["properties"]

@pytest.mark.asyncio
async def test_mcp_client_execute_tool(sample_config):
    """Test routing tool execution to the right session."""
    service = McpClientService(server_configs=sample_config)
    
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_content = MagicMock()
    mock_content.type = "text"
    mock_content.text = "Result of the query"
    mock_result.content = [mock_content]
    mock_result.isError = False
    
    mock_session.call_tool.return_value = mock_result
    service.sessions["sqlite-tools"] = mock_session
    
    # We call with the namespaced name
    result = await service.execute_tool("sqlite-tools", "query_db", {"query": "SELECT 1"})
    
    assert result == "Result of the query"
    mock_session.call_tool.assert_awaited_once_with("query_db", arguments={"query": "SELECT 1"})

@pytest.mark.asyncio
async def test_cleanup_all(sample_config):
    """Test context stack cleanup."""
    service = McpClientService(server_configs=sample_config)
    
    mock_exit_stack = AsyncMock()
    service.exit_stack = mock_exit_stack
    
    await service.cleanup_all()
    
    mock_exit_stack.aclose.assert_awaited_once()
    assert service.sessions == {}

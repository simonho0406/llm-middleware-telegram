import pytest
import sys
import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from telegram import Update
from telegram.ext import ContextTypes
from bot.response_generator import _generate_llm_response

# Helper for async iteration
async def async_iter(items):
    for item in items:
        yield item

@pytest.fixture
def mock_update_context():
    mock_update = MagicMock(spec=Update)
    mock_context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    mock_context.chat_data = {}
    mock_context.user_data = {}
    mock_context.application = MagicMock()
    mock_context.application.bot_data = {}
    return mock_update, mock_context

@pytest.mark.asyncio
async def test_recursive_tool_execution(mock_update_context):
    """
    Test Case 1: Recursive tool execution and history updating.
    Mock the LLM service to request a tool call first, and then return text on the second call.
    Assert that:
      - The orchestrator handles the recursion.
      - The tool is executed successfully.
      - Both the tool call and tool result are appended to the history and saved.
      - The final response is returned correctly.
    """
    mock_update, mock_context = mock_update_context
    chat_id = 12345
    prompt = "Get schema for sqlite-tools database"

    # Turn 0: LLM requests sqlite-tools__query_db
    tool_call_payload = {
        "tool_calls": [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "sqlite-tools__query_db",
                    "arguments": "{\"query\": \"SELECT name FROM sqlite_master\"}"
                }
            }
        ]
    }
    
    # Turn 1: LLM returns standard synthesized response
    final_text = "The database has the following tables: users, sessions."

    # Setup the mock generator outputs for each turn.
    # Use a pre-built iterator so next() advances through turns correctly.
    outputs_iter = iter([
        async_iter([json.dumps(tool_call_payload)]),
        async_iter([final_text])
    ])
    
    mock_service = MagicMock()
    mock_service.generate_response = MagicMock(side_effect=lambda **kwargs: next(outputs_iter))

    mock_provider_details = {
        "nvidia": {"service": mock_service, "default_model": "mock_model"},
    }

    # Setup MCP Client Service Mock
    mock_mcp = AsyncMock()
    mock_mcp.get_all_tools = AsyncMock(return_value=[{"type": "function", "function": {"name": "sqlite-tools__query_db"}}])
    mock_mcp.execute_tool = AsyncMock(return_value="['users', 'sessions']")
    mock_context.application.bot_data['mcp_service'] = mock_mcp

    # Setup storage manager mocks
    mock_get_history = AsyncMock(return_value=[])
    mock_save_msg = AsyncMock()
    mock_get_setting = AsyncMock(side_effect=lambda cid, key, default: True if key in ('enable_mcp', 'enable_skills') else False)

    with patch('bot.response_generator.providers.get_provider_details', return_value=mock_provider_details), \
         patch('storage.storage_manager.get_thread_history', mock_get_history), \
         patch('storage.storage_manager.save_message', mock_save_msg), \
         patch('storage.storage_manager.get_user_setting', mock_get_setting), \
         patch('storage.storage_manager.get_thread_key', AsyncMock(return_value="nvidia")), \
         patch('utils.hooks.hook_runner.run_pre_tool_use', MagicMock()):

        # Execute
        result = await _generate_llm_response(mock_context, chat_id, prompt)

        # Assertions
        assert result['content'] == final_text
        assert result['error'] is None
        
        # Verify McpClientService.execute_tool was called
        mock_mcp.execute_tool.assert_called_once_with("sqlite-tools", "query_db", {"query": "SELECT name FROM sqlite_master"})
        
        # Verify messages saved to database
        # Should save the user's initial prompt, then inside the loop:
        # assistant tool call message and the tool response message.
        # Check that mock_save_msg was called for assistant and tool
        save_calls = [call[0] for call in mock_save_msg.call_args_list]
        assistant_calls = [c for c in save_calls if c[1] == 'assistant']
        tool_calls = [c for c in save_calls if c[1] == 'tool']
        
        assert len(assistant_calls) >= 1
        assert len(tool_calls) >= 1

@pytest.mark.asyncio
async def test_user_settings_toggles(mock_update_context):
    """
    Test Case 2: User settings toggles (enable_mcp and enable_skills).
    Verify that when settings are toggled off:
      - Tools are not fetched from that subsystem.
      - Tools are not passed to generate_response.
    """
    mock_update, mock_context = mock_update_context
    chat_id = 12345
    prompt = "Test settings toggles"

    # Setup the mock service.
    # Use side_effect to produce a fresh generator on every call, preventing exhaustion.
    mock_service = MagicMock()
    mock_service.generate_response = MagicMock(side_effect=lambda **kwargs: async_iter(["Response"]))
    mock_provider_details = {"nvidia": {"service": mock_service, "default_model": "mock_model"}}

    # Mocks for MCP and Skills
    mock_mcp = AsyncMock()
    mock_mcp.get_all_tools = AsyncMock(return_value=[{"type": "function", "function": {"name": "mcp_tool"}}])
    mock_context.application.bot_data['mcp_service'] = mock_mcp

    mock_skills = MagicMock()
    mock_skills.get_skills_as_tools = MagicMock(return_value=[{"type": "function", "function": {"name": "skill_tool"}}])
    mock_context.application.bot_data['skill_service'] = mock_skills

    # Scenario A: both enabled
    mock_get_setting_both_enabled = AsyncMock(side_effect=lambda cid, key, default: True if key in ('enable_mcp', 'enable_skills') else False)
    
    with patch('bot.response_generator.providers.get_provider_details', return_value=mock_provider_details), \
         patch('storage.storage_manager.get_thread_history', AsyncMock(return_value=[])), \
         patch('storage.storage_manager.get_user_setting', mock_get_setting_both_enabled), \
         patch('storage.storage_manager.get_thread_key', AsyncMock(return_value="nvidia")), \
         patch('storage.storage_manager.save_message', AsyncMock()):

        await _generate_llm_response(mock_context, chat_id, prompt)
        
        # Verify both subsystems were queried for tools
        mock_mcp.get_all_tools.assert_called_once()
        mock_skills.get_skills_as_tools.assert_called_once()
        
        # Verify they were passed to generate_response
        called_args = mock_service.generate_response.call_args.kwargs
        assert "tools" in called_args
        assert len(called_args["tools"]) == 2

    # Reset call counts
    mock_mcp.get_all_tools.reset_mock()
    mock_skills.get_skills_as_tools.reset_mock()
    mock_service.generate_response.reset_mock()

    # Scenario B: enable_mcp=False, enable_skills=True
    mock_get_setting_mcp_disabled = AsyncMock(side_effect=lambda cid, key, default: False if key == 'enable_mcp' else True)
    
    with patch('bot.response_generator.providers.get_provider_details', return_value=mock_provider_details), \
         patch('storage.storage_manager.get_thread_history', AsyncMock(return_value=[])), \
         patch('storage.storage_manager.get_user_setting', mock_get_setting_mcp_disabled), \
         patch('storage.storage_manager.get_thread_key', AsyncMock(return_value="nvidia")), \
         patch('storage.storage_manager.save_message', AsyncMock()):

        await _generate_llm_response(mock_context, chat_id, prompt)
        
        # Verify only skills was queried
        mock_mcp.get_all_tools.assert_not_called()
        mock_skills.get_skills_as_tools.assert_called_once()
        
        # Verify only skill tool was passed
        called_args = mock_service.generate_response.call_args.kwargs
        assert len(called_args["tools"]) == 1
        assert called_args["tools"][0]["function"]["name"] == "skill_tool"

@pytest.mark.asyncio
async def test_safety_hooks_permission_failure(mock_update_context):
    """
    Test Case 3: Safety hooks permission failure interception.
    Assert that when a safety hook raises a PermissionError, the error message
    is intercepted and passed back to the LLM as a tool execution response.
    """
    mock_update, mock_context = mock_update_context
    chat_id = 12345
    prompt = "Delete system files"

    # Turn 0: LLM requests destructive tool
    destructive_tool_payload = {
        "tool_calls": [
            {
                "id": "call_666",
                "type": "function",
                "function": {
                    "name": "sqlite-tools__execute_query",
                    "arguments": "{\"query\": \"DROP TABLE users;\"}"
                }
            }
        ]
    }
    
    # Turn 1: LLM returns explanation based on the returned permission error
    final_text = "I cannot execute that command because access was denied: PermissionError."

    # Use a pre-built iterator so Turn 1 gets the final_text generator.
    outputs_iter = iter([
        async_iter([json.dumps(destructive_tool_payload)]),
        async_iter([final_text])
    ])
    
    mock_service = MagicMock()
    mock_service.generate_response = MagicMock(side_effect=lambda **kwargs: next(outputs_iter))
    mock_provider_details = {"nvidia": {"service": mock_service, "default_model": "mock_model"}}

    # Setup MCP client
    mock_mcp = AsyncMock()
    mock_mcp.get_all_tools = AsyncMock(return_value=[{"type": "function", "function": {"name": "sqlite-tools__execute_query"}}])
    mock_context.application.bot_data['mcp_service'] = mock_mcp

    # Hook runner raises PermissionError
    mock_hook_runner = MagicMock()
    mock_hook_runner.run_pre_tool_use.side_effect = PermissionError("Access Denied: Blocked command 'DROP TABLE' detected.")

    with patch('bot.response_generator.providers.get_provider_details', return_value=mock_provider_details), \
         patch('storage.storage_manager.get_thread_history', AsyncMock(return_value=[])), \
         patch('storage.storage_manager.get_user_setting', AsyncMock(return_value=True)), \
         patch('storage.storage_manager.get_thread_key', AsyncMock(return_value="nvidia")), \
         patch('storage.storage_manager.save_message', AsyncMock()) as mock_save_msg, \
         patch('utils.hooks.hook_runner.run_pre_tool_use', mock_hook_runner.run_pre_tool_use):

        result = await _generate_llm_response(mock_context, chat_id, prompt)

        # Assertions
        assert result['content'] == final_text
        
        # Verify MCP execute_tool was NEVER called due to hook rejection
        mock_mcp.execute_tool.assert_not_called()
        
        # Verify that the tool response message saved to DB contains the Permission Error text
        save_calls = [call[0] for call in mock_save_msg.call_args_list]
        tool_save_calls = [c for c in save_calls if c[1] == 'tool']
        
        assert len(tool_save_calls) == 1
        # The content of the tool save call should contain the permission error message
        assert "Access Denied" in tool_save_calls[0][2] or "PermissionError" in tool_save_calls[0][2]

@pytest.mark.asyncio
async def test_turn_limit_ceiling(mock_update_context):
    """
    Test Case 4: Turn limit ceiling (breaks at 5 turns with warning text).
    Force a mock tool call that always triggers another tool call.
    Assert that after 5 turns, the loop terminates cleanly and returns a warning.
    """
    mock_update, mock_context = mock_update_context
    chat_id = 12345
    prompt = "Run runaway tool loop"

    # Infinite tool loop payload
    loop_payload = {
        "tool_calls": [
            {
                "id": "call_loop",
                "type": "function",
                "function": {
                    "name": "sqlite-tools__loop_tool",
                    "arguments": "{}"
                }
            }
        ]
    }
    
    mock_service = MagicMock()
    # Always yield a fresh tool call generator on each turn to prevent single-use exhaustion.
    mock_service.generate_response = MagicMock(side_effect=lambda **kwargs: async_iter([json.dumps(loop_payload)]))
    mock_provider_details = {"nvidia": {"service": mock_service, "default_model": "mock_model"}}

    # Setup MCP client
    mock_mcp = AsyncMock()
    mock_mcp.get_all_tools = AsyncMock(return_value=[{"type": "function", "function": {"name": "sqlite-tools__loop_tool"}}])
    mock_mcp.execute_tool = AsyncMock(return_value="Loop response")
    mock_context.application.bot_data['mcp_service'] = mock_mcp

    with patch('bot.response_generator.providers.get_provider_details', return_value=mock_provider_details), \
         patch('storage.storage_manager.get_thread_history', AsyncMock(return_value=[])), \
         patch('storage.storage_manager.get_user_setting', AsyncMock(return_value=True)), \
         patch('storage.storage_manager.get_thread_key', AsyncMock(return_value="nvidia")), \
         patch('storage.storage_manager.save_message', AsyncMock()), \
         patch('utils.hooks.hook_runner.run_pre_tool_use', MagicMock()):

        result = await _generate_llm_response(mock_context, chat_id, prompt)

        # After 5 tool turns the loop forces a synthesis call (6th call, tools=None).
        # The mock still returns a tool-call payload, so synthesis fails and the
        # fallback warning message is emitted.
        assert "5-turn tool-call limit" in result['content']
        # 5 tool-loop turns + 1 forced synthesis attempt = 6 total service calls
        assert mock_service.generate_response.call_count == 6

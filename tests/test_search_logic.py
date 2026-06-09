import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from bot import response_generator

@pytest.mark.asyncio
async def test_search_instruction_logic():
    """Verify that search instruction is only added when enabled."""
    chat_id = 12345
    prompt = "Hello"
    
    # 1. Test DISABLED search
    with patch('bot.response_generator.storage_manager') as mock_storage:
        # Mock get_user_setting to return False (disabled)
        mock_storage.get_user_setting = AsyncMock(return_value=False)
        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_thread_key = AsyncMock(return_value='mock_provider')
        
        with patch('bot.response_generator.providers.get_provider_details') as mock_details:
             mock_service = MagicMock()
             async def async_gen(*args, **kwargs):
                 yield "Response"
             mock_service.generate_response = MagicMock(side_effect=async_gen)
             
             mock_details.return_value = {
                 'mock_provider': {'service': mock_service, 'default_model': 'm'}
             }
             
             with patch('bot.response_generator.ensure_context_fits', new_callable=AsyncMock) as mock_fits:
                 mock_fits.return_value = ([], "info")
                 
                 mock_mcp = AsyncMock()
                 mock_mcp.get_all_tools = AsyncMock(return_value=[])
                 mock_context = MagicMock()
                 mock_context.chat_data = {}
                 mock_context.application = MagicMock()
                 mock_context.application.bot_data = {'mcp_service': mock_mcp}
                 await response_generator._generate_llm_response(mock_context, chat_id, prompt)
                 
                 # VERIFY: prompt passed to service should NOT contain search instruction
                 call_args = mock_service.generate_response.call_args
                 _, kwargs = call_args
                 called_prompt = kwargs['prompt']
                 
                 assert "<search>" not in called_prompt
                 assert called_prompt == "Hello"

    # 2. Test ENABLED search
    with patch('bot.response_generator.storage_manager') as mock_storage:
        mock_storage.get_user_setting = AsyncMock(return_value=True) # ENABLED
        mock_storage.get_thread_history = AsyncMock(return_value=[])
        mock_storage.get_thread_key = AsyncMock(return_value='mock_provider')
        
        with patch('bot.response_generator.providers.get_provider_details') as mock_details:
             mock_service = MagicMock()
             async def async_gen(*args, **kwargs):
                 yield "Response"
             mock_service.generate_response = MagicMock(side_effect=async_gen)
             
             mock_details.return_value = {
                 'mock_provider': {'service': mock_service, 'default_model': 'm'}
             }
             
             with patch('bot.response_generator.ensure_context_fits', new_callable=AsyncMock) as mock_fits:
                 mock_fits.return_value = ([], "info")
                 
                 mock_mcp = AsyncMock()
                 mock_mcp.get_all_tools = AsyncMock(return_value=[])
                 mock_context = MagicMock()
                 mock_context.chat_data = {}
                 mock_context.application = MagicMock()
                 mock_context.application.bot_data = {'mcp_service': mock_mcp}
                 await response_generator._generate_llm_response(mock_context, chat_id, prompt)
                 
                 # VERIFY: prompt passed to service MUST contain search instruction
                 call_args = mock_service.generate_response.call_args
                 _, kwargs = call_args
                 called_prompt = kwargs['prompt']
                 
                 assert "<search>" in called_prompt

@pytest.mark.asyncio
async def test_panel_labeling():
    """Verify that assistant:panel role gets labeled."""
    chat_id = 999
    history = [{'role': 'assistant:panel', 'content': 'Panel Result'}]
    
    with patch('bot.response_generator.storage_manager') as mock_storage:
        mock_storage.get_user_setting = AsyncMock(return_value=False)
        mock_storage.get_thread_history = AsyncMock(return_value=history)
        mock_storage.get_thread_key = AsyncMock(return_value='mock_provider')
        
        with patch('bot.response_generator.providers.get_provider_details') as mock_details:
             mock_service = MagicMock()
             async def async_gen(*args, **kwargs):
                 yield "Response"
             mock_service.generate_response = MagicMock(side_effect=async_gen)
             
             mock_details.return_value = {
                 'mock_provider': {'service': mock_service, 'default_model': 'm'}
             }
             
             with patch('bot.response_generator.ensure_context_fits', new_callable=AsyncMock) as mock_fits:
                 mock_fits.return_value = ([], "info")
                 
                 mock_mcp = AsyncMock()
                 mock_mcp.get_all_tools = AsyncMock(return_value=[])
                 mock_context = MagicMock()
                 mock_context.chat_data = {}
                 mock_context.application = MagicMock()
                 mock_context.application.bot_data = {'mcp_service': mock_mcp}
                 await response_generator._generate_llm_response(mock_context, chat_id, "hi")
                 
                 # Inspect what was passed to ensure_context_fits (which receives processed_history)
                 call_args = mock_fits.call_args
                 _, kwargs = call_args
                 passed_history = kwargs['history']

                 # A system message is prepended; find the assistant:panel entry by role
                 panel_msg = next((m for m in passed_history if m['role'] == 'assistant'), None)
                 assert panel_msg is not None, "No assistant message found in passed_history"
                 assert "**[Previous Expert Panel Discussion Result]**" in panel_msg['content']

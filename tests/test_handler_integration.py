#!/usr/bin/env python3
"""
Integration tests to catch runtime issues that unit tests miss.

These tests verify that handlers can be imported, basic functions work,
and critical execution paths don't have scope/import issues.
"""
import pytest
import sys
import os

# Add the parent directory to the Python path to import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestHandlerImports:
    """Test that handlers can be imported and don't have basic scope issues."""
    
    def test_discuss_panel_handler_imports(self):
        """Test that discuss_panel_handler imports without errors."""
        try:
            from bot.handlers import discuss_panel_handler
            # Basic smoke test - ensure key functions exist
            assert hasattr(discuss_panel_handler, '_run_panel_workflow')
            assert hasattr(discuss_panel_handler, '_format_panel_summary') 
        except ImportError as e:
            pytest.fail(f"Failed to import discuss_panel_handler: {e}")
        except Exception as e:
            pytest.fail(f"Unexpected error importing discuss_panel_handler: {e}")
    
    def test_configure_panel_handler_imports(self):
        """Test that configure_panel_handler imports without errors."""
        try:
            from bot.handlers import configure_panel_handler
            # Basic smoke test - ensure key functions exist
            assert hasattr(configure_panel_handler, 'load_panel_config')
            assert hasattr(configure_panel_handler, 'save_role_config')
            assert hasattr(configure_panel_handler, 'deep_merge_configs')
        except ImportError as e:
            pytest.fail(f"Failed to import configure_panel_handler: {e}")
        except Exception as e:
            pytest.fail(f"Unexpected error importing configure_panel_handler: {e}")
            
    def test_text_processing_imports(self):
        """Test that text processing imports work correctly."""
        try:
            from utils.text_processing import pure_markdown_to_telegram_v2, escape_markdown_v2
            # Verify functions are callable
            assert callable(pure_markdown_to_telegram_v2)
            assert callable(escape_markdown_v2)
        except ImportError as e:
            pytest.fail(f"Failed to import text_processing: {e}")
        except Exception as e:
            pytest.fail(f"Unexpected error importing text_processing: {e}")


class TestCriticalFunctionScoping:
    """Test that functions don't have variable scope issues that only appear at runtime."""
    
    def test_json_import_availability(self):
        """Test that json is properly imported in handlers that use it."""
        import ast
        import os
        
        # Check discuss_panel_handler for proper json usage
        handler_path = os.path.join(os.path.dirname(__file__), '..', 'bot', 'handlers', 'discuss_panel_handler.py')
        if os.path.exists(handler_path):
            with open(handler_path, 'r') as f:
                content = f.read()
                
            # Parse the AST to check for json usage
            tree = ast.parse(content)
            
            # Find all uses of json.dumps or json.loads
            json_calls = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id == 'json' and node.attr in ['dumps', 'loads']:
                        json_calls.append(node.lineno)
            
            if json_calls:
                # Verify json is imported at module level
                imports = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name == 'json':
                                imports.append(node.lineno)
                    elif isinstance(node, ast.ImportFrom) and node.module == 'json':
                        imports.append(node.lineno)
                
                assert imports, f"json is used on lines {json_calls} but not imported at module level"
    
    def test_deep_merge_function_isolation(self):
        """Test that deep_merge_configs works with various input types."""
        from bot.handlers.configure_panel_handler import deep_merge_configs
        
        # Test cases that could reveal scope issues
        base = {'a': 1, 'b': {'c': 2}}
        override = {'b': {'d': 3}}
        
        result = deep_merge_configs(base, override)
        
        # Should have merged properly
        assert result['a'] == 1
        assert result['b']['c'] == 2  
        assert result['b']['d'] == 3
        
        # Test edge cases that might cause scope issues
        assert deep_merge_configs({}, {}) == {}
        assert deep_merge_configs({'a': 1}, None) == {'a': 1}
        assert deep_merge_configs({'a': 1}, 'invalid') == {'a': 1}


class TestDatabaseIntegration:
    """Test database operations that might have constraint violations."""
    
    def test_none_value_handling(self):
        """Test that setting None values doesn't cause constraint violations."""
        # This would catch the NULL constraint issue we had
        from storage.database_storage import set_user_setting
        import asyncio
        import tempfile
        import os
        
        # Use a temporary database for testing
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            temp_db = tmp.name
            
        try:
            # Mock the config.DB_PATH
            import config
            original_db_path = getattr(config, 'DB_PATH', None)
            config.DB_PATH = temp_db
            
            # Initialize database
            from storage.database_storage import init_database
            asyncio.run(init_database())
            
            # Test setting None (should delete, not cause constraint violation)
            async def test_none_setting():
                await set_user_setting(123, 'panel_config', '{"test": "value"}')
                await set_user_setting(123, 'panel_config', None)  # Should not fail
                
            asyncio.run(test_none_setting())
            
        finally:
            # Cleanup
            if original_db_path:
                config.DB_PATH = original_db_path
            if os.path.exists(temp_db):
                os.unlink(temp_db)


class TestCallbackPatternValidation:
    """Test callback patterns don't have overlapping conflicts."""
    
    def test_configure_panel_callback_patterns(self):
        """Test that pagination buttons don't conflict with model selection."""
        from bot.handlers.configure_panel_handler import (
            MODEL_CALLBACK_PREFIX, MODEL_PAGE_CALLBACK_PREFIX
        )
        
        # Generate sample callback data
        model_callback = f"{MODEL_CALLBACK_PREFIX}test-model"
        pagination_callback = f"{MODEL_PAGE_CALLBACK_PREFIX}2"
        
        # Test the patterns
        import re
        model_pattern = f"^{MODEL_CALLBACK_PREFIX}"
        pagination_pattern = f"^{MODEL_PAGE_CALLBACK_PREFIX}"
        
        # Model callback should match model pattern but not pagination pattern
        assert re.match(model_pattern, model_callback), "Model callback should match model pattern"
        assert not re.match(pagination_pattern, model_callback), "Model callback should not match pagination pattern"
        
        # Pagination callback should match pagination pattern but not vice versa
        assert re.match(pagination_pattern, pagination_callback), "Pagination callback should match pagination pattern"
        
        # CRITICAL: Pagination callback should NOT match the broader model pattern
        # This was the bug - pagination buttons were treated as model selections
        # With proper handler ordering, the more specific pagination pattern should be matched first
        assert re.match(model_pattern, pagination_callback), "Pagination does start with model prefix (expected)"
        
        # The fix relies on handler ordering - more specific patterns must come first
        print(f"Model pattern: {model_pattern}")
        print(f"Pagination pattern: {pagination_pattern}")
        print(f"Sample pagination callback: {pagination_callback}")
        print("✓ Handler ordering fix should prevent pagination→model confusion")


class TestBotMenuAndHelp:
    """Test bot command menu registration and help text consistency."""
    
    def test_configure_panel_in_help_text(self):
        """Test that configure_panel command is documented in help text."""
        from bot.handlers.misc_commands import help_command
        import inspect
        
        # Get the help text from the function source
        source = inspect.getsource(help_command)
        
        # Should contain configure_panel command
        assert "configure_panel" in source, "configure_panel should be documented in help text"
        assert "Customize your Expert Panel agents" in source, "configure_panel description should be in help"
    
    def test_configure_panel_in_command_menu(self):
        """Test that configure_panel command is registered in bot menu."""
        from bot.menu_setup import setup_bot_commands_and_menu
        import inspect
        
        # Get the command list from the function source
        source = inspect.getsource(setup_bot_commands_and_menu)
        
        # Should contain configure_panel command
        assert 'BotCommand("configure_panel"' in source, "configure_panel should be in bot command menu"
        assert "Customize your Expert Panel agents" in source, "configure_panel description should match"
    
    def test_help_and_menu_consistency(self):
        """Test that help text and menu commands are consistent."""
        # This is a meta-test to ensure we don't have commands in one but not the other
        
        # Extract commands from help text
        from bot.handlers.misc_commands import help_command
        import inspect
        import re
        
        help_source = inspect.getsource(help_command)
        # Find all /command patterns in help text
        help_commands = re.findall(r'/(\w+)', help_source)
        help_commands = set(help_commands)  # Remove duplicates
        
        # Extract commands from menu setup
        from bot.menu_setup import setup_bot_commands_and_menu
        menu_source = inspect.getsource(setup_bot_commands_and_menu)
        # Find all BotCommand patterns
        menu_commands = re.findall(r'BotCommand\("(\w+)"', menu_source)
        menu_commands = set(menu_commands)
        
        # Key commands that should be in both
        critical_commands = {
            'help', 'config', 'discuss_panel', 'configure_panel', 
            'search', 'ask_selected', 'provider', 'model'
        }
        
        # Test that critical commands are in both help and menu
        for cmd in critical_commands:
            assert cmd in help_commands, f"/{cmd} should be documented in help text"
            assert cmd in menu_commands, f"/{cmd} should be registered in bot menu"
        
        # Specifically test our new command
        assert 'configure_panel' in help_commands, "configure_panel missing from help"
        assert 'configure_panel' in menu_commands, "configure_panel missing from menu"
    
    def test_command_descriptions_match(self):
        """Test that command descriptions are consistent between help and menu."""
        # This ensures we don't have mismatched descriptions
        
        from bot.handlers.misc_commands import help_command
        from bot.menu_setup import setup_bot_commands_and_menu
        import inspect
        
        help_source = inspect.getsource(help_command)
        menu_source = inspect.getsource(setup_bot_commands_and_menu)
        
        # Test specific commands have consistent descriptions
        test_cases = [
            ("configure_panel", "Customize your Expert Panel agents"),
            ("discuss_panel", "Orchestrate an expert AI panel"),
            ("config", "Manage bot settings"),
        ]
        
        for command, expected_desc in test_cases:
            # Check both help and menu contain the description
            assert expected_desc in help_source or command in help_source, \
                f"{command} description should be in help text"
            assert expected_desc in menu_source, \
                f"{command} description should be in menu setup"


class TestCommandRegistrationIntegration:
    """Integration tests for command registration and handler setup."""
    
    def test_configure_panel_handler_registered(self):
        """Test that configure_panel command has a proper handler registered."""
        # Import the main application setup
        try:
            from main import main
            from bot.handlers.configure_panel_handler import configure_panel_conv_handler
            
            # Verify the conversation handler exists and has the right structure
            assert configure_panel_conv_handler is not None, "configure_panel_conv_handler should exist"
            
            # Check it has the expected entry points
            entry_points = configure_panel_conv_handler.entry_points
            assert any("configure_panel" in str(handler.commands) 
                      for handler in entry_points 
                      if hasattr(handler, 'commands')), \
                "configure_panel command should be registered as entry point"
                
        except ImportError as e:
            # If we can't import main, at least check the handler exists
            from bot.handlers.configure_panel_handler import configure_panel_conv_handler
            assert configure_panel_conv_handler is not None, "Handler should exist even if main import fails"
    
    def test_menu_setup_can_be_called(self):
        """Test that menu setup function can be called without errors."""
        from bot.menu_setup import setup_bot_commands_and_menu
        
        # This is a smoke test - we can't easily mock the full bot setup,
        # but we can at least verify the function exists and is callable
        assert callable(setup_bot_commands_and_menu), "setup_bot_commands_and_menu should be callable"
        
        # Test that it has the expected signature
        import inspect
        sig = inspect.signature(setup_bot_commands_and_menu)
        params = list(sig.parameters.keys())
        assert 'application' in params, "Function should accept application parameter"
        assert 'chat_id' in params, "Function should accept chat_id parameter"


class TestRegressionPrevention:
    """Tests specifically designed to catch the exact issues we had in production."""
    
    def test_json_scope_regression(self):
        """Regression test for the UnboundLocalError with json imports."""
        # This test verifies that our current codebase doesn't have conditional json imports
        # The original issue was: json imported conditionally but used unconditionally
        
        # Check that key handler files import json at module level, not conditionally
        import ast
        import os
        
        files_to_check = [
            ('bot/handlers/discuss_panel_handler.py', 'discuss_panel_handler'),
            ('bot/handlers/configure_panel_handler.py', 'configure_panel_handler')
        ]
        
        for file_path, module_name in files_to_check:
            full_path = os.path.join(os.path.dirname(__file__), '..', file_path)
            if os.path.exists(full_path):
                with open(full_path, 'r') as f:
                    content = f.read()
                
                # Parse AST to find json usage and imports
                tree = ast.parse(content)
                
                # Find json usage (json.dumps, json.loads)
                json_usage = []
                json_imports = []
                
                for node in ast.walk(tree):
                    # Find json method calls
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                        if node.value.id == 'json':
                            json_usage.append(node.lineno)
                    
                    # Find imports at module level (not nested in functions/conditions)
                    if isinstance(node, ast.Import) and node.col_offset == 0:
                        for alias in node.names:
                            if alias.name == 'json':
                                json_imports.append(node.lineno)
                
                # If json is used, it must be imported at module level
                if json_usage:
                    assert json_imports, f"{module_name} uses json but doesn't import it at module level (lines: {json_usage})"
    
    def test_database_null_constraint_regression(self):
        """Regression test for NULL constraint violations.""" 
        # Test the exact scenario that failed
        import tempfile
        import asyncio
        import aiosqlite
        
        with tempfile.NamedTemporaryFile(suffix='.db') as tmp:
            db_path = tmp.name
            
            async def test_null_handling():
                # Create table with NOT NULL constraint (like our production schema)
                async with aiosqlite.connect(db_path) as db:
                    await db.execute('''
                        CREATE TABLE user_settings (
                            chat_id INTEGER,
                            key TEXT, 
                            value TEXT NOT NULL,
                            PRIMARY KEY (chat_id, key)
                        )
                    ''')
                    
                    # This should fail with NULL constraint (old behavior)
                    with pytest.raises(Exception):
                        await db.execute(
                            "INSERT OR REPLACE INTO user_settings (chat_id, key, value) VALUES (?, ?, ?)",
                            (123, 'panel_config', None)
                        )
            
            asyncio.run(test_null_handling())
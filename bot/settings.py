# File: bot/settings.py
"""Central registry for user-configurable settings."""

USER_SETTINGS = {
    'autosearch_chat': {
        'display_name': "Auto-Search in Chat",
        'description': "Automatically search the web when the AI detects it needs current information.",
        'type': bool,
        'default': True,
    },
    'autosearch_panel': {
        'display_name': "Auto-Search in Panels",
        'description': "Allow Expert Panels to search the web during discussions.",
        'type': bool,
        'default': True,
    },
    'advanced_search_panel': {
        'display_name': "Advanced Search in Panels",
        'description': "Use deep-dive multi-query search strategy in panels. Slower but more thorough.",
        'type': bool,
        'default': False,
    },
    'inject_history_in_panel': {
        'display_name': "Inject History in Panels",
        'description': "Include your conversation history as context when starting a panel discussion.",
        'type': bool,
        'default': False,
    },
    'auto_retry_on_error': {
        'display_name': "Auto-Retry on Error",
        'description': "Automatically retry once when the AI provider drops the connection mid-response.",
        'type': bool,
        'default': True,
    },
    'panel_config': {
        'display_name': "Expert Panel Configuration",
        'type': dict,
        'default': None,
    },
    'enable_mcp': {
        'display_name': "Enable MCP Tools",
        'description': "Enable Model Context Protocol (MCP) server tools for LLMs.",
        'type': bool,
        'default': True,
    },
    'enable_skills': {
        'display_name': "Enable Skills",
        'description': "Enable local system skills / script playbooks for LLMs.",
        'type': bool,
        'default': True,
    },
}
# File: bot/settings.py
"""Central registry for user-configurable settings."""

USER_SETTINGS = {
    'autosearch_chat': {
        'display_name': "Auto-Search in Chat",
        'type': bool,
        'default': True,
    },
    'autosearch_panel': {
        'display_name': "Auto-Search in Panels",
        'type': bool,
        'default': True,
    },
    'advanced_search_panel': {
        'display_name': "Advanced Search in Panels",
        'type': bool,
        'default': False,
    },
    'inject_history_in_panel': {
        'display_name': "Inject History in Panels",
        'type': bool,
        'default': False,
    },
    'panel_config': {
        'display_name': "Expert Panel Configuration",
        'type': dict,
        'default': None,
    },
}
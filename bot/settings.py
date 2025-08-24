# File: bot/settings.py
"""Central registry for user-configurable settings."""

USER_SETTINGS = {
    'autosearch_normal_chat': {
        'display_name': "Auto-Search in Chat",
        'type': bool,
        'default': True,
    },
    'autosearch_panel_discussion': {
        'display_name': "Auto-Search in Panels",
        'type': bool,
        'default': True,
    },
}
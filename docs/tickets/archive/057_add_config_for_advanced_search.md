
# TICKET-057: Add Configuration for Advanced Search

**Status:** Open
**Priority:** Medium

## Goal

To add a user-facing setting in the `/config` menu to enable or disable the "Conversational Research" feature, ensuring it's off by default to conserve API calls and reduce default latency.

## Key Insights & Context

- New features, especially those that increase latency and cost, should always be opt-in to respect user choice and resources.
- The implementation should be a simple conditional check at the beginning of the research loop in `_run_panel_workflow`.

## Acceptance Criteria (TDD Plan)

1.  A new setting, `advanced_search_panel`, will be added to `config.yaml` (defaulting to `false`) and to the `bot/settings.py` `USER_SETTINGS` structure.
2.  The integration test from Ticket 056 will be parameterized to run twice:
    - **Case 1 (Enabled):** Asserts that the research loop functions are called.
    - **Case 2 (Disabled):** Asserts that the research loop functions are **not** called and the workflow falls back to the old, simple search behavior.
3.  The `/config` command handler and its associated callbacks will be updated to allow users to toggle this new setting on and off.


# TICKET-011: Update Dependencies for `markdown-it-py` Migration

**Status:** CLOSED

**Problem:** We are migrating our Markdown rendering engine. The first step is to update the project's dependencies.

**Definition of Done:**
1.  In `requirements.txt`, **remove** the line `mistletoe-ebp`.
2.  In `requirements.txt`, **add** the following two lines:
    ```
    markdown-it-py
    mdit-py-plugins
    ```
3.  Run `docker compose build` to confirm the new dependencies install correctly.

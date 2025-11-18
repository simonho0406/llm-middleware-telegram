# Standalone Scripts

This directory contains standalone, one-off scripts that are used for development, maintenance, or data migration. These scripts are not part of the main application's runtime and should be executed manually with caution.

- `migrate_json_to_sqlite.py`: A script to migrate user sessions and settings from the old `sessions.json` file to the new SQLite database (`bot_sessions.db`).
- `migrate_db.py`: A utility script for database maintenance tasks, such as cleaning up old or invalid records.

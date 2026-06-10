# 027 — Schema migration runs on every polling-loop restart under load

## Severity: Medium

## Problem

`storage/database_storage.py:init_database` is invoked from
`post_init_with_commands` on every iteration of `main.py`'s polling
loop. `_migrate_messages_table` does:

1. `PRAGMA foreign_keys = OFF`
2. `CREATE TABLE messages_new (...)` (with the new schema)
3. `INSERT INTO messages_new SELECT ... FROM messages`
4. `DROP TABLE messages`
5. `ALTER TABLE messages_new RENAME TO messages`
6. `PRAGMA foreign_keys = ON`

The defensive `DROP TABLE IF EXISTS messages_new` on line 116 already
acknowledges this isn't atomic.

## Failure mode

Bot restarts (NetworkError → polling loop re-entry) **while a chat
handler from the previous iteration is mid-`save_message`**. Both
hold separate `aiosqlite.connect()` handles on the same WAL DB.
Possible outcomes:
- The chat-handler INSERT lands on the original `messages` table,
  which gets DROP'd after the migration's `INSERT...SELECT`. **Row
  is lost.**
- The chat-handler INSERT fails with `database is locked` because
  the migration is in a transaction.
- The migration's INSERT...SELECT and the chat-handler INSERT
  interleave under WAL — partial visibility.

## Peer issue (also affected)

`_migrate_user_settings_table` (database_storage.py:157) has the same shape
— DROP + recreate + re-insert without `BEGIN EXCLUSIVE`, runs on every
boot. Same data-loss window for the `user_settings` table. Fix together.

## Fix direction

Detect whether the migration is needed first
(`PRAGMA table_info(messages)` → check `content` nullability). Only
run rebuild on actual mismatch. Wrap the rebuild in
`BEGIN EXCLUSIVE` to serialize. Better: move migration to a
separate offline script (Alembic) executed before bot start, not
on every boot.

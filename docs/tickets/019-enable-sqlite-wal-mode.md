# Ticket 019: Enable SQLite WAL Mode for Concurrent Safety

**Priority:** P2 — Performance / Reliability  
**Type:** Enhancement  
**Branch:** N/A (apply to `main` post-merge)  
**Status:** Open  
**Created:** 2026-05-01  
**Discovered By:** Ticket 017 Post-Refactor Code Review  

---

## Context

The application uses `aiosqlite` with a connection-per-call pattern (`async with aiosqlite.connect()` in every function). SQLite's default journal mode (`DELETE`) only allows one writer at a time, and readers block writers.

With `concurrent_updates=True` enabled in the Telegram application, multiple handlers can execute simultaneously. While `aiosqlite` uses a background thread per connection (preventing asyncio blocking), concurrent writes to the same database can still result in `SQLITE_BUSY` errors under load.

## Proposed Fix

Add WAL (Write-Ahead Logging) mode to `init_database()` in `storage/database_storage.py`:

```python
async def init_database():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")  # ← ADD THIS
        await db.execute("PRAGMA foreign_keys = ON;")
        # ... rest of init
```

## Benefits

- **Concurrent reads + writes**: WAL mode allows multiple readers while a single writer is active — eliminates most `SQLITE_BUSY` scenarios
- **Better crash recovery**: WAL provides faster recovery after unexpected shutdowns
- **No schema changes required**: WAL mode is a PRAGMA setting, not a structural change
- **Backwards compatible**: The database file format remains compatible

## Risks

- WAL creates two additional files (`-wal` and `-shm`) alongside the database file
- Slightly increased disk usage (typically negligible)
- WAL mode persists across connections once set — this is intentional and desirable

## Verification

```bash
# After running the bot once with the change:
sqlite3 data/chat_history.db "PRAGMA journal_mode;"
# Should output: wal
```

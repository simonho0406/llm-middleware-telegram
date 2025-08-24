#!/usr/bin/env python3
"""
Debug utility to inspect and clean persistent database states that cause
callback query routing failures in the Telegram bot.

Usage:
    python debug_persistence.py [--clean-db] [--check-schema] [--list-data]
"""

import asyncio
import argparse
import sqlite3
import os
import sys

# Add the project root to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from storage import database_storage


async def check_database_schema():
    """Check the current database schema for inconsistencies."""
    print("=== Database Schema Analysis ===")
    
    if not os.path.exists(config.DB_PATH):
        print(f"Database not found at: {config.DB_PATH}")
        return
    
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    # Check user_settings table schema
    cursor.execute("PRAGMA table_info(user_settings)")
    columns = cursor.fetchall()
    
    print("user_settings table schema:")
    for col in columns:
        print(f"  {col[1]} {col[2]} {'NOT NULL' if col[3] else ''} {'PRIMARY KEY' if col[5] else ''}")
    
    # Check for data type issues
    value_column_type = None
    for col in columns:
        if col[1] == 'value':
            value_column_type = col[2]
            break
    
    if value_column_type:
        print(f"\nvalue column type: {value_column_type}")
        if value_column_type.upper() == 'TEXT':
            print("WARNING: value column is TEXT but code expects INTEGER")
        else:
            print("OK: value column type is correct")
    
    conn.close()


async def list_database_data():
    """List all data in the database."""
    print("=== Database Contents ===")
    
    if not os.path.exists(config.DB_PATH):
        print(f"Database not found at: {config.DB_PATH}")
        return
    
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    # List chats
    cursor.execute("SELECT * FROM chats")
    chats = cursor.fetchall()
    print(f"Chats ({len(chats)}):")
    for chat in chats:
        print(f"  chat_id: {chat[0]}, current_thread: {chat[1]}")
    
    # List threads
    cursor.execute("SELECT * FROM threads")
    threads = cursor.fetchall()
    print(f"\nThreads ({len(threads)}):")
    for thread in threads:
        print(f"  pk: {thread[0]}, chat: {thread[1]}, id: {thread[2]}, name: {thread[3]}")
    
    # List user_settings
    cursor.execute("SELECT * FROM user_settings")
    settings = cursor.fetchall()
    print(f"\nUser Settings ({len(settings)}):")
    for setting in settings:
        print(f"  chat: {setting[0]}, key: {setting[1]}, value: {setting[2]} ({type(setting[2]).__name__})")
    
    # List messages count
    cursor.execute("SELECT COUNT(*) FROM messages")
    msg_count = cursor.fetchone()[0]
    print(f"\nTotal Messages: {msg_count}")
    
    conn.close()


async def clean_database():
    """Clean persistent states that might interfere with ConversationHandlers."""
    print("=== Cleaning Database States ===")
    
    if not os.path.exists(config.DB_PATH):
        print(f"Database not found at: {config.DB_PATH}")
        return
    
    # Initialize storage to run migrations if needed
    await database_storage.init_database()
    print("OK: Database initialization and migration completed")
    
    # The migrations in init_database() should handle the schema fixes
    print("OK: Database cleanup completed")


async def main():
    parser = argparse.ArgumentParser(description="Debug bot persistence issues")
    parser.add_argument("--clean-db", action="store_true", help="Clean persistent database states")
    parser.add_argument("--check-schema", action="store_true", help="Check database schema")
    parser.add_argument("--list-data", action="store_true", help="List database contents")
    
    args = parser.parse_args()
    
    if not any([args.clean_db, args.check_schema, args.list_data]):
        # Default: run all checks
        await check_database_schema()
        print()
        await list_database_data()
        print()
        await clean_database()
    else:
        if args.check_schema:
            await check_database_schema()
            print()
        
        if args.list_data:
            await list_database_data()
            print()
        
        if args.clean_db:
            await clean_database()
            print()


if __name__ == "__main__":
    asyncio.run(main())
"""Shared pytest fixtures."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest

import config


@pytest.fixture
def isolated_db_path(tmp_path):
    """Point config.DB_PATH at a throwaway SQLite file for the duration of one test,
    restoring the original in a finally block so a failure mid-test never leaks the
    temp path to other tests.

    Does NOT call init_database() — migration tests need to seed an old schema first,
    while storage tests init before use. Opt in by requesting the fixture; it is not
    autouse, so unrelated tests keep the real DB path.
    """
    db_path = str(tmp_path / "test.db")
    original = config.DB_PATH
    config.DB_PATH = db_path
    try:
        yield db_path
    finally:
        config.DB_PATH = original

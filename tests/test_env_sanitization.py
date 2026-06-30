"""
Tests for config.get_env — defensive stripping of surrounding quotes/whitespace from
env values. Docker's env_file/--env-file passes a quoted .env value (KEY="abc") literally,
unlike python-dotenv, silently corrupting API keys (auth 401). get_env normalizes both.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import patch

import config


@pytest.mark.parametrize("raw,expected", [
    ('gsk_realkey', 'gsk_realkey'),          # already clean → unchanged
    ('"gsk_realkey"', 'gsk_realkey'),         # double-quoted (the Docker env_file case)
    ("'gsk_realkey'", 'gsk_realkey'),         # single-quoted
    ('  gsk_realkey  ', 'gsk_realkey'),       # stray whitespace (CRLF artifacts)
    ('"gsk_realkey"\t', 'gsk_realkey'),       # trailing whitespace outside quotes
    ('', ''),                                 # empty stays empty
])
def test_get_env_strips_quotes_and_whitespace(raw, expected):
    with patch.dict(os.environ, {"SOME_KEY": raw}):
        assert config.get_env("SOME_KEY") == expected


def test_get_env_missing_returns_default():
    with patch.dict(os.environ, {}, clear=True):
        assert config.get_env("DEFINITELY_MISSING", "fallback") == "fallback"
        assert config.get_env("DEFINITELY_MISSING") is None


def test_get_env_does_not_strip_inner_quotes():
    # Only SURROUNDING quotes are stripped; a quote inside the value is preserved.
    with patch.dict(os.environ, {"K": 'ab"cd'}):
        assert config.get_env("K") == 'ab"cd'

"""
Tests for bot.middleware.auth_middleware — fail-closed access control.

The repo is public and the bot multi-user, so the access gate must DENY by default:
no `allowed_chat_ids` and no `open_access` ⇒ every chat is rejected. These pin that
invariant (a regression to fail-open would re-expose the bot to the world).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import MagicMock, patch
from telegram.ext import ApplicationHandlerStop

from bot import middleware


def _update(chat_id=555):
    u = MagicMock()
    u.effective_chat.id = chat_id
    u.effective_user.id = chat_id
    return u


@pytest.fixture(autouse=True)
def _reset_throttle():
    # Reset module-level log throttles so tests don't interfere.
    middleware._misconfig_warned = False
    middleware._last_denied_log_ts = 0.0
    yield


@pytest.mark.asyncio
async def test_deny_by_default_when_unconfigured():
    """No allowlist + open_access off ⇒ every chat denied (fail-closed)."""
    with patch.object(middleware.config, "get_open_access", return_value=False), \
         patch.object(middleware.config, "get_allowed_chat_ids", return_value=None):
        with pytest.raises(ApplicationHandlerStop):
            await middleware.auth_middleware(_update(), MagicMock())


@pytest.mark.asyncio
async def test_open_access_allows_any_chat():
    """Explicit open_access: true ⇒ allowed (no exception)."""
    with patch.object(middleware.config, "get_open_access", return_value=True), \
         patch.object(middleware.config, "get_allowed_chat_ids", return_value=None):
        await middleware.auth_middleware(_update(9999), MagicMock())  # must not raise


@pytest.mark.asyncio
async def test_allowlisted_chat_passes():
    with patch.object(middleware.config, "get_open_access", return_value=False), \
         patch.object(middleware.config, "get_allowed_chat_ids", return_value=[555]):
        await middleware.auth_middleware(_update(555), MagicMock())  # must not raise


@pytest.mark.asyncio
async def test_non_allowlisted_chat_denied():
    with patch.object(middleware.config, "get_open_access", return_value=False), \
         patch.object(middleware.config, "get_allowed_chat_ids", return_value=[111]):
        with pytest.raises(ApplicationHandlerStop):
            await middleware.auth_middleware(_update(555), MagicMock())


@pytest.mark.asyncio
async def test_no_chat_id_is_ignored_not_denied():
    """An update with no chat (e.g. inline) is a no-op, not a hard deny."""
    u = MagicMock()
    u.effective_chat = None
    u.effective_user = None
    with patch.object(middleware.config, "get_open_access", return_value=False), \
         patch.object(middleware.config, "get_allowed_chat_ids", return_value=None):
        await middleware.auth_middleware(u, MagicMock())  # must not raise


# ── is_chat_allowed: single source of truth shared with recovery (review #8) ─────

def test_is_chat_allowed_fail_closed():
    import config
    with patch.object(config, "get_open_access", return_value=False), \
         patch.object(config, "get_allowed_chat_ids", return_value=None):
        assert config.is_chat_allowed(123) is False        # unconfigured → deny
    with patch.object(config, "get_open_access", return_value=True), \
         patch.object(config, "get_allowed_chat_ids", return_value=None):
        assert config.is_chat_allowed(123) is True          # open_access → allow
    with patch.object(config, "get_open_access", return_value=False), \
         patch.object(config, "get_allowed_chat_ids", return_value=[123]):
        assert config.is_chat_allowed(123) is True          # allowlisted
        assert config.is_chat_allowed(999) is False         # not allowlisted


# ── allowlist sourced from .env (gitignored), not committed config.yaml ──────────

def test_allowed_chat_ids_from_env():
    import config
    with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "842443019, -100123 , bad, 55"}):
        # env parsed to ints; non-integer entries ignored; quotes/whitespace tolerated
        assert config.get_allowed_chat_ids() == [842443019, -100123, 55]


def test_allowed_chat_ids_env_takes_precedence_over_yaml():
    import config
    with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "111"}), \
         patch.dict(config._yaml_config, {"allowed_chat_ids": [999]}, clear=False):
        assert config.get_allowed_chat_ids() == [111]


def test_allowed_chat_ids_falls_back_to_yaml_when_env_unset():
    import config
    with patch.dict(os.environ, {}, clear=True), \
         patch.dict(config._yaml_config, {"allowed_chat_ids": [777]}, clear=False):
        assert config.get_allowed_chat_ids() == [777]


@pytest.mark.parametrize("val,expected", [("true", True), ("1", True), ("yes", True),
                                          ("on", True), ("false", False), ("", False)])
def test_open_access_from_env(val, expected):
    import config
    with patch.dict(os.environ, {"OPEN_ACCESS": val}):
        assert config.get_open_access() is expected

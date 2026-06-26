"""
Tests for bot.messaging.send_safe_message — the centralized delivery path that renders
text through the MarkdownV2 AST pipeline and, on failure, falls back to chunked plain
text. Previously untested end-to-end; this pins the resilience contract:

  * happy path renders MarkdownV2;
  * a BadRequest (bad entities) falls back to plain text and still succeeds;
  * 'message is not modified' is swallowed as success (no fallback);
  * if BOTH the AST send and the plain fallback fail, return False;
  * the plain fallback chunks to TELEGRAM_MAX_LEN.

Headless mode (update=None + chat_id) is used so we don't need a full Update mock.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from telegram import constants
from telegram.error import BadRequest

from bot.messaging import send_safe_message, TELEGRAM_MAX_LEN


def _ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.edit_message_text = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_happy_path_renders_markdown_v2():
    ctx = _ctx()
    result = await send_safe_message(ctx, None, "Hello **world**", chat_id=123)

    assert result is True
    ctx.bot.send_message.assert_awaited()
    assert ctx.bot.send_message.call_args.kwargs['parse_mode'] == constants.ParseMode.MARKDOWN_V2


@pytest.mark.asyncio
async def test_badrequest_falls_back_to_plain_text():
    ctx = _ctx()
    # First (MarkdownV2) send raises a parse error; the plain fallback succeeds.
    ctx.bot.send_message = AsyncMock(side_effect=[BadRequest("Can't parse entities"), None])

    result = await send_safe_message(ctx, None, "Hello world", chat_id=123)

    assert result is True
    assert ctx.bot.send_message.await_count == 2
    # The fallback send used no parse mode (raw text).
    assert ctx.bot.send_message.call_args_list[1].kwargs['parse_mode'] is None


@pytest.mark.asyncio
async def test_message_not_modified_is_swallowed_as_success():
    ctx = _ctx()
    ctx.bot.send_message = AsyncMock(side_effect=BadRequest("Message is not modified"))

    result = await send_safe_message(ctx, None, "Hello", chat_id=123)

    assert result is True
    # Swallowed — no plain-text fallback attempt.
    assert ctx.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_returns_false_when_both_paths_fail():
    ctx = _ctx()
    ctx.bot.send_message = AsyncMock(side_effect=Exception("network down"))

    result = await send_safe_message(ctx, None, "Hello", chat_id=123)

    assert result is False
    # MarkdownV2 attempt + plain fallback attempt both made.
    assert ctx.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_plain_fallback_chunks_to_telegram_max_len():
    ctx = _ctx()
    long_text = "A" * (TELEGRAM_MAX_LEN + 500)  # → 2 chunks in the fallback

    # Force the AST pipeline to fail so we exercise the plain fallback path.
    with patch('bot.messaging.parse_markdown_to_ast', side_effect=ValueError("ast boom")):
        result = await send_safe_message(ctx, None, long_text, chat_id=123)

    assert result is True
    assert ctx.bot.send_message.await_count == 2
    for call in ctx.bot.send_message.call_args_list:
        assert call.kwargs['parse_mode'] is None
        assert len(call.kwargs['text']) <= TELEGRAM_MAX_LEN


@pytest.mark.asyncio
async def test_empty_text_sends_nothing():
    ctx = _ctx()
    result = await send_safe_message(ctx, None, "", chat_id=123)
    assert result is None
    ctx.bot.send_message.assert_not_awaited()

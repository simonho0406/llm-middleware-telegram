"""
Regression test for the asyncio.shield around the assistant-message save in
bot.response_generator._generate_and_send_response_task (response_generator.py:826).

Invariant: the assistant reply has already been sent to the user, so if /cancel arrives
between send and save, the save MUST still complete — otherwise the user sees a reply
that never entered history and /reroll shows the wrong turn. The shield guarantees the
inner save runs to completion even though the outer task is cancelled (and the task still
surfaces CancelledError to its wrapper).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.response_generator import _generate_and_send_response_task


@pytest.mark.asyncio
async def test_assistant_save_survives_cancellation():
    save_started = asyncio.Event()
    save_finished = asyncio.Event()
    release = asyncio.Event()
    saved = {}

    async def fake_save(chat_id, role, content, *args, **kwargs):
        if role == "assistant":
            save_started.set()
            await release.wait()          # hold the shielded save open
            saved["assistant"] = content
            save_finished.set()
            return 99
        return 1                          # user-input save

    with patch("bot.response_generator.storage_manager.save_message", new=fake_save), \
         patch("bot.response_generator.storage_manager.remove_last_assistant_message", new=AsyncMock()), \
         patch("bot.response_generator._generate_llm_response",
               new=AsyncMock(return_value={"content": "the answer", "error": None})), \
         patch("bot.response_generator.send_safe_message", new=AsyncMock(return_value=True)):

        task = asyncio.create_task(_generate_and_send_response_task(
            update=MagicMock(), context=MagicMock(), chat_id=123, user_id=456,
            prompt="hi", current_thread_id="default",
        ))

        # Wait until execution is inside the shielded assistant save, then cancel.
        await asyncio.wait_for(save_started.wait(), timeout=2.0)
        task.cancel()
        release.set()  # allow the shielded save to run to completion

        # The outer task surfaces cancellation to its wrapper...
        with pytest.raises(asyncio.CancelledError):
            await task

        # ...but the shielded save still completed.
        await asyncio.wait_for(save_finished.wait(), timeout=2.0)
        assert saved.get("assistant") == "the answer"

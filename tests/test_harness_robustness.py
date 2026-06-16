"""
Tests for the robustness harness: a user turn must end in an answer OR a visible
error — never silence — plus startup take-over of stranded turns.

Covers:
  * Layer 1 — _generate_and_send_response surfaces faults / unexpected cancels,
    and stays silent on an *expected* cancel.
  * Layer 2 — the global error_handler notifies even for JobQueue-originated
    errors (update is None, chat_id comes from context.job).
  * Layer 3 — the inactivity watchdog aborts a stalled stream but not a slow-
    but-progressing one.
  * Recovery — reconcile_unanswered_messages resumes a recent stranded user
    message, ignores old ones, and skips already-answered threads.
"""
import asyncio
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import bot.response_generator as rg


# ───────────────────────── Layer 1: outer guard ─────────────────────────

@pytest.mark.asyncio
async def test_layer1_exception_is_surfaced():
    context = MagicMock()
    context.chat_data = {}
    update = MagicMock()

    async def boom(*a, **k):
        raise ValueError("boom")

    with patch.object(rg, '_generate_and_send_response_task', boom), \
         patch.object(rg, '_notify_user_failure', new_callable=AsyncMock) as notify:
        await rg._generate_and_send_response(update, context, 1, 2, "hi", "t1")
        notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_layer1_unexpected_cancel_is_surfaced():
    context = MagicMock()
    context.chat_data = {}
    update = MagicMock()

    async def cancel_me(*a, **k):
        raise asyncio.CancelledError()

    with patch.object(rg, '_generate_and_send_response_task', cancel_me), \
         patch.object(rg, '_notify_user_failure', new_callable=AsyncMock) as notify:
        await rg._generate_and_send_response(update, context, 1, 2, "hi", "t1")
        notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_layer1_expected_cancel_is_silent():
    context = MagicMock()
    context.chat_data = {}
    update = MagicMock()

    async def expected_cancel(*a, **k):
        # Simulate a deliberate canceller having flagged this task before cancelling.
        asyncio.current_task()._expected_cancel = True
        raise asyncio.CancelledError()

    with patch.object(rg, '_generate_and_send_response_task', expected_cancel), \
         patch.object(rg, '_notify_user_failure', new_callable=AsyncMock) as notify:
        await rg._generate_and_send_response(update, context, 1, 2, "hi", "t1")
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_layer1_success_is_silent():
    context = MagicMock()
    context.chat_data = {}
    update = MagicMock()

    async def ok(*a, **k):
        return None

    with patch.object(rg, '_generate_and_send_response_task', ok), \
         patch.object(rg, '_notify_user_failure', new_callable=AsyncMock) as notify:
        await rg._generate_and_send_response(update, context, 1, 2, "hi", "t1")
        notify.assert_not_awaited()


# ───────────────────────── Layer 2: error_handler ───────────────────────

@pytest.mark.asyncio
async def test_layer2_job_error_notifies_via_job_chat_id():
    import main

    update = object()  # NOT a telegram Update — mimics a JobQueue-originated error
    context = MagicMock()
    context.error = ValueError("kaboom")
    job = MagicMock()
    job.chat_id = 555
    job.data = {}
    context.job = job

    with patch('bot.messaging.send_plain_message', new_callable=AsyncMock) as spm:
        await main.error_handler(update, context)
        spm.assert_awaited()
        # send_plain_message(context, chat_id, text) — chat_id resolved from the job
        assert spm.await_args.args[1] == 555


@pytest.mark.asyncio
async def test_layer2_no_target_does_not_send():
    import main

    update = object()
    context = MagicMock()
    context.error = ValueError("x")
    context.job = None

    with patch('bot.messaging.send_plain_message', new_callable=AsyncMock) as spm:
        await main.error_handler(update, context)
        spm.assert_not_awaited()


# ───────────────────────── Layer 3: inactivity watchdog ─────────────────

def _patch_generation_plumbing(service, provider_config=None):
    """Common patches so _generate_llm_response can run with a fake service."""
    provider_info = {'provider': 'x', 'provider_display': 'X', 'model': 'm', 'service': service}
    sm = MagicMock()
    sm.get_thread_history = AsyncMock(return_value=[])
    sm.get_user_setting = AsyncMock(return_value=False)  # autosearch/mcp/skills/auto_retry all off
    # get_or_init_mcp_service / _skill_service are imported function-locally and
    # short-circuit to None when the user setting is off (get_user_setting → False),
    # so they need no patching here.
    return patch.multiple(
        rg,
        storage_manager=sm,
        _get_provider_configuration=AsyncMock(
            return_value=('x', 'm', provider_config or {'enable_streaming': True}, service, provider_info)
        ),
        ensure_context_fits=AsyncMock(return_value=([{"role": "system", "content": "sys"}], "")),
        send_draft_message=AsyncMock(),
        finalize_draft=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_watchdog_aborts_stalled_stream():
    class StallService:
        async def generate_response(self, **kwargs):
            await asyncio.sleep(0.5)  # exceeds the 0.1s idle budget below
            yield "too late"

    context = MagicMock()
    context.application = MagicMock()

    with _patch_generation_plumbing(StallService()), \
         patch.object(rg.config, 'get_enable_streaming', return_value=True), \
         patch.object(rg.config, 'get_generation_idle_timeout_seconds', return_value=0.1), \
         patch.object(rg.config, 'get_storage_backend', return_value='database'), \
         patch.object(rg.config, 'PROMPTS') as prompts:
        prompts.get_prompt.return_value = "sys"
        result = await rg._generate_llm_response(context, 123, "hello")

    assert result['error'] == 'llm_error'
    assert "stopped responding" in result['content']


@pytest.mark.asyncio
async def test_watchdog_allows_progressing_stream():
    class FastService:
        async def generate_response(self, **kwargs):
            for i in range(3):
                await asyncio.sleep(0.01)  # well within the idle budget
                yield f"tok{i} "

    context = MagicMock()
    context.application = MagicMock()

    with _patch_generation_plumbing(FastService()), \
         patch.object(rg.config, 'get_enable_streaming', return_value=True), \
         patch.object(rg.config, 'get_generation_idle_timeout_seconds', return_value=0.1), \
         patch.object(rg.config, 'get_storage_backend', return_value='database'), \
         patch.object(rg.config, 'PROMPTS') as prompts:
        prompts.get_prompt.return_value = "sys"
        result = await rg._generate_llm_response(context, 123, "hello")

    assert result['error'] is None
    assert "tok0" in result['content']


# ───────────────────────── Recovery: startup take-over ──────────────────

import bot.recovery as recovery


def _recovery_storage(history):
    sm = MagicMock()
    sm.get_all_chat_ids = AsyncMock(return_value=[123])
    sm.get_thread_history_with_pk = AsyncMock(return_value=history)
    sm.get_current_thread_id = AsyncMock(return_value='t1')
    sm.delete_messages = AsyncMock()
    return sm


@pytest.mark.asyncio
async def test_recovery_resumes_recent_stranded_message():
    now = int(time.time())
    sm = _recovery_storage([{'id': 9, 'role': 'user', 'content': 'do x', 'timestamp': now - 60}])

    with patch.object(recovery, 'storage_manager', sm), \
         patch.object(recovery.config, 'get_recovery_enabled', return_value=True), \
         patch.object(recovery.config, 'get_recovery_window_seconds', return_value=3600), \
         patch('bot.messaging.send_plain_message', new_callable=AsyncMock) as spm, \
         patch('telegram.ext.CallbackContext', MagicMock()), \
         patch('bot.response_generator._generate_and_send_response', new_callable=AsyncMock) as gen:
        resumed = await recovery.reconcile_unanswered_messages(MagicMock())

    assert resumed == 1
    gen.assert_awaited_once()
    # The existing user row is resumed in place (save_input=False), NOT deleted —
    # deleting then re-saving would risk losing the message on a crash mid-recovery.
    assert gen.await_args.kwargs.get('save_input') is False
    sm.delete_messages.assert_not_awaited()
    spm.assert_awaited()  # "Catching up…" notice


@pytest.mark.asyncio
async def test_recovery_skips_panel_followup():
    # A stranded message that followed a panel turn must NOT be resumed as normal chat.
    now = int(time.time())
    sm = _recovery_storage([
        {'id': 8, 'role': 'assistant:panel', 'content': 'panel answer', 'timestamp': now - 120},
        {'id': 9, 'role': 'user', 'content': 'panel follow-up', 'timestamp': now - 60},
    ])
    with patch.object(recovery, 'storage_manager', sm), \
         patch.object(recovery.config, 'get_recovery_enabled', return_value=True), \
         patch.object(recovery.config, 'get_recovery_window_seconds', return_value=3600), \
         patch('bot.messaging.send_plain_message', new_callable=AsyncMock), \
         patch('bot.response_generator._generate_and_send_response', new_callable=AsyncMock) as gen:
        resumed = await recovery.reconcile_unanswered_messages(MagicMock())
    assert resumed == 0
    gen.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_skips_when_live_task_running():
    # Recovery must not pile a second generation onto a chat that already has one live.
    now = int(time.time())
    sm = _recovery_storage([{'id': 9, 'role': 'user', 'content': 'do x', 'timestamp': now - 60}])
    live = MagicMock()
    live.done.return_value = False
    app = MagicMock()
    app.chat_data = {123: {'llm_task': live}}
    app.user_data = {}
    with patch.object(recovery, 'storage_manager', sm), \
         patch.object(recovery.config, 'get_recovery_enabled', return_value=True), \
         patch.object(recovery.config, 'get_recovery_window_seconds', return_value=3600), \
         patch('bot.messaging.send_plain_message', new_callable=AsyncMock), \
         patch('bot.response_generator._generate_and_send_response', new_callable=AsyncMock) as gen:
        resumed = await recovery.reconcile_unanswered_messages(app)
    assert resumed == 0
    gen.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_ignores_old_message():
    now = int(time.time())
    sm = _recovery_storage([{'id': 9, 'role': 'user', 'content': 'do x', 'timestamp': now - 7200}])

    with patch.object(recovery, 'storage_manager', sm), \
         patch.object(recovery.config, 'get_recovery_enabled', return_value=True), \
         patch.object(recovery.config, 'get_recovery_window_seconds', return_value=3600), \
         patch('bot.messaging.send_plain_message', new_callable=AsyncMock), \
         patch('bot.response_generator._generate_and_send_response', new_callable=AsyncMock) as gen:
        resumed = await recovery.reconcile_unanswered_messages(MagicMock())

    assert resumed == 0
    gen.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_skips_answered_thread():
    now = int(time.time())
    sm = _recovery_storage([{'id': 9, 'role': 'assistant', 'content': 'done', 'timestamp': now - 60}])

    with patch.object(recovery, 'storage_manager', sm), \
         patch.object(recovery.config, 'get_recovery_enabled', return_value=True), \
         patch.object(recovery.config, 'get_recovery_window_seconds', return_value=3600), \
         patch('bot.response_generator._generate_and_send_response', new_callable=AsyncMock) as gen:
        resumed = await recovery.reconcile_unanswered_messages(MagicMock())

    assert resumed == 0
    gen.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_disabled_noop():
    sm = _recovery_storage([{'id': 9, 'role': 'user', 'content': 'do x', 'timestamp': int(time.time())}])
    with patch.object(recovery, 'storage_manager', sm), \
         patch.object(recovery.config, 'get_recovery_enabled', return_value=False), \
         patch('bot.response_generator._generate_and_send_response', new_callable=AsyncMock) as gen:
        resumed = await recovery.reconcile_unanswered_messages(MagicMock())
    assert resumed == 0
    gen.assert_not_awaited()

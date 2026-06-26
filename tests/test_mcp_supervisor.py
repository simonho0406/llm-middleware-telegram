"""
Unit tests for utils.service_registry._mcp_supervisor — the long-lived task that owns
the entire MCP service lifecycle (connect, keep-alive, idle shutdown, final cleanup).

This subsystem had zero coverage and is memory-critical: it spawns/reaps MCP subprocesses
(~150-200 MB). We drive the REAL supervisor coroutine with a recording fake
McpClientService, a shrunk tick, and an instant backoff, so timing stays sub-second.

Invariants pinned here:
  * request → ready: connect once, commit the service, clear the request signal.
  * connect failure → retry: request_event stays set; the supervisor retries (ticket 028).
  * idle → cleanup in the supervisor's OWN task (the asyncio-safe property), service reset.
  * shutdown / cancellation → final cleanup_all runs.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest

import utils.service_registry as sr

# Captured before any patching so our poller is immune to a patched asyncio.sleep.
_real_sleep = asyncio.sleep


async def _wait_until(cond, timeout=2.0):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await _real_sleep(0.005)
    return cond()


def _make_fake_mcp(fail_times=0, observe_event=None):
    """Returns (FakeMcpClass, state). `state['connect_attempts']` counts connect_all
    calls across all instances; `fail_times` of them raise before succeeding.

    If `observe_event` is given, each connect attempt records that event's set-state
    into state['req_set_per_attempt'] — used to prove request_event stays set across
    a connect failure (ticket 028)."""
    state = {'connect_attempts': 0, 'instances': [], 'req_set_per_attempt': []}

    class FakeMcp:
        def __init__(self, server_configs):
            self.cleanup_calls = 0
            self.cleanup_task = None
            state['instances'].append(self)

        async def connect_all(self):
            state['connect_attempts'] += 1
            if observe_event is not None:
                state['req_set_per_attempt'].append(observe_event.is_set())
            if state['connect_attempts'] <= fail_times:
                raise RuntimeError("connect boom")

        async def cleanup_all(self):
            self.cleanup_calls += 1
            self.cleanup_task = asyncio.current_task()

    return FakeMcp, state


def _make_app():
    return SimpleNamespace(bot_data={
        'mcp_request_event': asyncio.Event(),
        'mcp_ready_event': asyncio.Event(),
        'mcp_shutdown_event': asyncio.Event(),
        'mcp_service': None,
        'mcp_last_used': 0.0,
    })


async def _shutdown(app, task):
    """Signal shutdown and await the supervisor; cancel as a last resort."""
    app.bot_data['mcp_shutdown_event'].set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.fixture(autouse=True)
def _fast_tick():
    with patch.object(sr, '_SUPERVISOR_TICK_SECONDS', 0.01):
        yield


@pytest.mark.asyncio
async def test_request_to_ready_commits_service():
    FakeMcp, state = _make_fake_mcp()
    app = _make_app()

    with patch('services.mcp_service.McpClientService', FakeMcp):
        # Huge idle window so the service is not cleaned up mid-test.
        task = asyncio.create_task(sr._mcp_supervisor(app, idle_seconds=10**9))
        app.bot_data['mcp_request_event'].set()

        await asyncio.wait_for(app.bot_data['mcp_ready_event'].wait(), timeout=2.0)

        assert app.bot_data['mcp_service'] is state['instances'][0]
        assert state['connect_attempts'] == 1
        assert not app.bot_data['mcp_request_event'].is_set(), "request cleared after success"

        await _shutdown(app, task)


@pytest.mark.asyncio
async def test_connect_failure_then_retry_succeeds():
    app = _make_app()
    # observe_event lets the fake record request_event's state on each connect attempt.
    FakeMcp, state = _make_fake_mcp(fail_times=1, observe_event=app.bot_data['mcp_request_event'])

    # Instant backoff so the failure-path asyncio.sleep(5) doesn't stall the test.
    with patch('services.mcp_service.McpClientService', FakeMcp), \
         patch('asyncio.sleep', new=AsyncMock()):
        task = asyncio.create_task(sr._mcp_supervisor(app, idle_seconds=10**9))
        app.bot_data['mcp_request_event'].set()

        committed = await _wait_until(lambda: app.bot_data['mcp_service'] is not None)
        assert committed, "service was never committed after a retry"
        assert state['connect_attempts'] >= 2

        # Ticket 028: request_event must remain SET across the first failure so the
        # retry actually fires — pin it directly, not just via "a retry happened".
        assert state['req_set_per_attempt'][0] is True
        assert state['req_set_per_attempt'][1] is True, "request_event was cleared after the failed connect"
        # Only cleared once the connect finally succeeds.
        assert not app.bot_data['mcp_request_event'].is_set()

        await _shutdown(app, task)


@pytest.mark.asyncio
async def test_idle_triggers_cleanup_in_supervisor_task():
    FakeMcp, state = _make_fake_mcp()
    app = _make_app()

    with patch('services.mcp_service.McpClientService', FakeMcp):
        # idle_seconds=0 → service is reaped immediately after connecting.
        task = asyncio.create_task(sr._mcp_supervisor(app, idle_seconds=0))
        app.bot_data['mcp_request_event'].set()

        reaped = await _wait_until(lambda: app.bot_data['mcp_service'] is None
                                   and state['instances'] and state['instances'][0].cleanup_calls == 1)
        assert reaped, "idle service was not cleaned up"

        fake = state['instances'][0]
        assert fake.cleanup_task is task, "cleanup must run in the supervisor's own task"
        assert not app.bot_data['mcp_ready_event'].is_set()

        await _shutdown(app, task)


@pytest.mark.asyncio
async def test_shutdown_cleans_up_and_returns():
    FakeMcp, state = _make_fake_mcp()
    app = _make_app()

    with patch('services.mcp_service.McpClientService', FakeMcp):
        task = asyncio.create_task(sr._mcp_supervisor(app, idle_seconds=10**9))
        app.bot_data['mcp_request_event'].set()
        await asyncio.wait_for(app.bot_data['mcp_ready_event'].wait(), timeout=2.0)
        fake = state['instances'][0]

        app.bot_data['mcp_shutdown_event'].set()
        await asyncio.wait_for(task, timeout=2.0)  # returns cleanly, no exception

        assert task.done() and task.exception() is None
        assert fake.cleanup_calls == 1
        assert app.bot_data['mcp_service'] is None


@pytest.mark.asyncio
async def test_cancellation_runs_final_cleanup():
    FakeMcp, state = _make_fake_mcp()
    app = _make_app()

    with patch('services.mcp_service.McpClientService', FakeMcp):
        task = asyncio.create_task(sr._mcp_supervisor(app, idle_seconds=10**9))
        app.bot_data['mcp_request_event'].set()
        await asyncio.wait_for(app.bot_data['mcp_ready_event'].wait(), timeout=2.0)
        fake = state['instances'][0]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.cleanup_calls == 1, "final cleanup must run on cancellation"
        assert app.bot_data['mcp_service'] is None

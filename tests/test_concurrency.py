"""
Tests for utils.concurrency — the global generation cap that bounds peak RAM/CPU on
small VMs. Verifies the semaphore actually limits concurrency and rebinds per event loop.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
import pytest
from unittest.mock import patch

import utils.concurrency as concurrency


@pytest.fixture(autouse=True)
def _reset_sem():
    concurrency._sem = None
    concurrency._sem_loop = None
    yield
    concurrency._sem = None
    concurrency._sem_loop = None


@pytest.mark.asyncio
async def test_run_capped_limits_concurrency():
    """With limit=2, no more than 2 wrapped coroutines run at once."""
    live = 0
    peak = 0
    gate = asyncio.Event()

    async def work():
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await gate.wait()
        live -= 1

    with patch.object(concurrency.config, "get_max_concurrent_generations", return_value=2):
        tasks = [asyncio.create_task(concurrency.run_capped(work())) for _ in range(5)]
        await asyncio.sleep(0.05)          # let as many start as the semaphore allows
        assert peak <= 2, f"semaphore breached: {peak} concurrent"
        assert live == 2                    # exactly the limit are running; 3 queued
        gate.set()
        await asyncio.gather(*tasks)
    assert peak == 2


@pytest.mark.asyncio
async def test_semaphore_rebinds_per_event_loop():
    """A semaphore from a previous loop must not leak into a new loop."""
    with patch.object(concurrency.config, "get_max_concurrent_generations", return_value=3):
        sem1 = concurrency.get_generation_semaphore()
        assert concurrency.get_generation_semaphore() is sem1  # stable within a loop

    # Simulate a fresh polling loop (as happens on NetworkError restart).
    concurrency._sem = None
    concurrency._sem_loop = None
    with patch.object(concurrency.config, "get_max_concurrent_generations", return_value=3):
        sem2 = concurrency.get_generation_semaphore()
    assert sem2 is not sem1


@pytest.mark.asyncio
async def test_run_capped_releases_on_exception():
    """A failing wrapped coro must release its permit (no permit leak)."""
    with patch.object(concurrency.config, "get_max_concurrent_generations", return_value=1):
        async def boom():
            raise ValueError("x")
        with pytest.raises(ValueError):
            await concurrency.run_capped(boom())
        # If the permit leaked, this second acquire would hang; wait_for guards it.
        async def ok():
            return 42
        assert await asyncio.wait_for(concurrency.run_capped(ok()), timeout=1.0) == 42

"""
Idempotent, race-free accessors for shared MCP and Skill services.

## MCP supervisor pattern

The MCP SDK's `stdio_client()` wraps stdio subprocesses in `anyio` cancel scopes
that enforce a strict invariant: the same asyncio task that *entered* a scope
must be the one to *exit* it. If `connect_all()` runs in Task A and any other
task (e.g. an APScheduler job, the post_shutdown_hook) calls `cleanup_all()`,
anyio raises `RuntimeError: Attempted to exit cancel scope in a different task`.
That leaves subprocesses as zombies and corrupts the asyncio event loop state.

To respect that invariant, the entire MCP service lifecycle (connect_all,
keep-alive, idle shutdown, final cleanup) is owned by a single long-lived
**supervisor task** spawned on first use. Callers communicate with it via
asyncio events:

  * `mcp_request_event` — set by callers wanting the service
  * `mcp_ready_event`   — set by supervisor when the service is connected
  * `mcp_shutdown_event`— set by bot shutdown to terminate the supervisor

The supervisor handles its own idle timeout internally (no external watchdog
needed). When idle for `idle_seconds`, it runs `cleanup_all()` in its own task
context (safe) and goes back to waiting for the next request. On the next
request, it reconnects transparently.

When `app is None` (QA/test contexts), services are created fresh and returned
unregistered — callers are responsible for cleanup in that case.
"""
import asyncio
import logging
import time

import config

logger = logging.getLogger(__name__)

# Idle threshold before the supervisor shuts the MCP subprocesses down to free
# memory. The supervisor reconnects transparently on the next request.
_DEFAULT_MCP_IDLE_SECONDS = 30 * 60

# How often the supervisor wakes to check for idle/shutdown.
_SUPERVISOR_TICK_SECONDS = 60


async def _mcp_supervisor(app, idle_seconds: int) -> None:
    """Long-lived task owning the entire MCP service lifecycle.

    Loop structure:
      1. Wait for a request event OR shutdown event
      2. If shutdown: exit. If request: connect_all(), set ready_event
      3. Tick every `_SUPERVISOR_TICK_SECONDS` checking idle / shutdown
      4. On idle: cleanup_all() in THIS task (safe), then back to step 1
      5. On shutdown: cleanup_all() and return
    """
    from services.mcp_service import McpClientService

    request_event: asyncio.Event = app.bot_data['mcp_request_event']
    ready_event: asyncio.Event = app.bot_data['mcp_ready_event']
    shutdown_event: asyncio.Event = app.bot_data['mcp_shutdown_event']

    try:
        while True:
            # ── Phase 1: wait for a request or shutdown ────────────────────
            if app.bot_data.get('mcp_service') is None:
                req_task = asyncio.create_task(request_event.wait())
                shut_task = asyncio.create_task(shutdown_event.wait())
                done, pending = await asyncio.wait(
                    {req_task, shut_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()

                if shutdown_event.is_set():
                    logger.info("MCP supervisor: shutdown signal received (no service to clean up).")
                    return

                request_event.clear()

                # ── Phase 2: connect ───────────────────────────────────────
                logger.info("MCP supervisor: connecting to all configured servers...")
                server_configs = config._yaml_config.get("mcp_servers", [])
                svc = McpClientService(server_configs)
                try:
                    await svc.connect_all()
                except Exception as e:
                    logger.error(f"MCP supervisor: connect_all failed: {e}")
                    # Signal callers so they don't block forever; mcp_service stays None
                    ready_event.set()
                    await asyncio.sleep(5)  # avoid tight reconnect loop
                    ready_event.clear()
                    continue

                app.bot_data['mcp_service'] = svc
                app.bot_data['mcp_last_used'] = time.monotonic()
                ready_event.set()
                logger.info("MCP supervisor: service ready.")

            # ── Phase 3: idle-check loop ───────────────────────────────────
            should_cleanup = False
            should_return = False
            while True:
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=_SUPERVISOR_TICK_SECONDS)
                except asyncio.TimeoutError:
                    pass

                if shutdown_event.is_set():
                    should_cleanup = True
                    should_return = True
                    break

                last_used = app.bot_data.get('mcp_last_used', 0)
                idle_for = time.monotonic() - last_used
                if idle_for >= idle_seconds:
                    logger.info(
                        f"MCP supervisor: service idle for {idle_for/60:.0f} min — "
                        f"shutting down subprocesses to free ~150-200 MB. "
                        f"Will reconnect on next request."
                    )
                    should_cleanup = True
                    break

            # ── Phase 4: cleanup in supervisor's own task ──────────────────
            if should_cleanup:
                svc = app.bot_data.get('mcp_service')
                app.bot_data['mcp_service'] = None
                ready_event.clear()
                if svc is not None:
                    try:
                        await svc.cleanup_all()
                    except Exception as e:
                        logger.warning(f"MCP supervisor: non-fatal cleanup error: {e}")

            if should_return:
                logger.info("MCP supervisor: exiting on shutdown signal.")
                return
            # Otherwise loop back to wait for next request

    except asyncio.CancelledError:
        logger.info("MCP supervisor: cancelled; running final cleanup.")
        svc = app.bot_data.get('mcp_service')
        app.bot_data['mcp_service'] = None
        if svc is not None:
            try:
                await svc.cleanup_all()
            except Exception as e:
                logger.warning(f"MCP supervisor: cleanup error during cancellation: {e}")
        raise


async def get_or_init_mcp_service(app, enable_mcp: bool):
    """Return the shared McpClientService, starting the supervisor if needed.

    Args:
        app:        The PTB Application instance, or None in QA/test contexts.
        enable_mcp: Whether MCP is enabled for this chat/session.

    Returns:
        A connected McpClientService, or None if disabled or connect failed.
    """
    if not enable_mcp:
        return None

    from services.mcp_service import McpClientService

    server_configs = config._yaml_config.get("mcp_servers", [])

    if app is None:
        # QA/test context — create a fresh unregistered instance
        svc = McpClientService(server_configs)
        await svc.connect_all()
        return svc

    # Pre-injected service (e.g. unit-test fixtures that put a mock in bot_data
    # before any call): don't take ownership, just return what's there.
    # The supervisor only manages services it created itself.
    if (
        app.bot_data.get('mcp_service') is not None
        and 'mcp_supervisor_task' not in app.bot_data
    ):
        return app.bot_data['mcp_service']

    # Initialize supervisor on first call (under the init lock)
    if 'mcp_init_lock' not in app.bot_data:
        app.bot_data['mcp_init_lock'] = asyncio.Lock()
    async with app.bot_data['mcp_init_lock']:
        if 'mcp_supervisor_task' not in app.bot_data:
            app.bot_data['mcp_request_event'] = asyncio.Event()
            app.bot_data['mcp_ready_event'] = asyncio.Event()
            app.bot_data['mcp_shutdown_event'] = asyncio.Event()
            app.bot_data['mcp_last_used'] = 0.0
            app.bot_data['mcp_supervisor_task'] = asyncio.create_task(
                _mcp_supervisor(app, idle_seconds=_DEFAULT_MCP_IDLE_SECONDS),
                name="mcp_supervisor",
            )
            logger.info("MCP supervisor task spawned.")

    # Touch BEFORE requesting so the supervisor sees a fresh last_used
    app.bot_data['mcp_last_used'] = time.monotonic()

    # Fast path: service already ready
    if (
        app.bot_data.get('mcp_service') is not None
        and app.bot_data['mcp_ready_event'].is_set()
    ):
        return app.bot_data['mcp_service']

    # Slow path: signal supervisor and wait for ready
    app.bot_data['mcp_request_event'].set()
    await app.bot_data['mcp_ready_event'].wait()
    return app.bot_data.get('mcp_service')


async def get_or_init_skill_service(app, enable_skills: bool):
    """Return the shared SkillRegistryService, initializing it exactly once."""
    if not enable_skills:
        return None

    from services.skill_service import SkillRegistryService

    skills_dir = config._yaml_config.get("skills_dir", "skills")

    if app is None:
        svc = SkillRegistryService(skills_dir=skills_dir)
        svc.load_skills()
        return svc

    if app.bot_data.get('skill_service') is not None:
        return app.bot_data['skill_service']

    if 'skill_init_lock' not in app.bot_data:
        app.bot_data['skill_init_lock'] = asyncio.Lock()

    async with app.bot_data['skill_init_lock']:
        if app.bot_data.get('skill_service') is not None:
            return app.bot_data['skill_service']

        logger.info("Initializing Skill registry service...")
        svc = SkillRegistryService(skills_dir=skills_dir)
        svc.load_skills()
        app.bot_data['skill_service'] = svc
        logger.info("Skill service initialized and registered in bot_data.")
        return svc


def touch_mcp_last_used(app) -> None:
    """Refresh the MCP last-used timestamp just before a tool call executes."""
    if app is not None and app.bot_data.get('mcp_service') is not None:
        app.bot_data['mcp_last_used'] = time.monotonic()


async def shutdown_mcp_supervisor(app, timeout: float = 15.0) -> None:
    """Signal the MCP supervisor to terminate cleanly, then await its exit.

    Called from cleanup_services on bot shutdown. Safe to call multiple times.
    """
    if app is None:
        return

    shutdown_event = app.bot_data.get('mcp_shutdown_event')
    sup_task = app.bot_data.get('mcp_supervisor_task')

    if shutdown_event is None or sup_task is None:
        return  # Supervisor never started

    if shutdown_event.is_set() and sup_task.done():
        return  # Already shut down

    shutdown_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(sup_task), timeout=timeout)
        logger.info("MCP supervisor exited cleanly.")
    except asyncio.TimeoutError:
        logger.warning(
            f"MCP supervisor did not exit within {timeout}s; cancelling. "
            f"Subprocesses may remain until OS reaps them."
        )
        sup_task.cancel()
        try:
            await asyncio.wait_for(sup_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    except Exception as e:
        logger.warning(f"Error awaiting MCP supervisor shutdown: {e}")

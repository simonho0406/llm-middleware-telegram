"""
Idempotent, race-free accessors for shared MCP and Skill services.

Both bot/response_generator.py and bot/handlers/discuss_panel_handler.py need
MCP/Skill services. The naïve pattern — check bot_data, initialize if None —
has a race condition: two concurrent coroutines can both observe None, both call
connect_all(), and spawn duplicate subprocesses. Only the last write to bot_data
survives; the first instance is leaked.

These helpers use double-checked locking with asyncio.Lock stored in bot_data.
The event loop is single-threaded, so the lock prevents interleaving at the
single await boundary (connect_all / load_skills). They are safe to call
concurrently from any number of coroutines.

When app is None (QA scripts, test harnesses) the service is created fresh and
returned unregistered — callers are responsible for cleanup in that case.
"""
import asyncio
import logging
import time

import config

logger = logging.getLogger(__name__)

# How long (seconds) the MCP service must be idle before the watchdog shuts it down.
# Configurable via config.yaml key `mcp_idle_timeout_seconds`; defaults to 30 min.
_DEFAULT_MCP_IDLE_SECONDS = 30 * 60


async def get_or_init_mcp_service(app, enable_mcp: bool):
    """Return the shared McpClientService, initializing it exactly once.

    Args:
        app:        The PTB Application instance, or None in QA/test contexts.
        enable_mcp: Whether MCP is enabled for this chat/session.

    Returns:
        A connected McpClientService, or None if enable_mcp is False.
    """
    if not enable_mcp:
        return None

    from services.mcp_service import McpClientService

    server_configs = config._yaml_config.get("mcp_servers", [])

    if app is None:
        # QA / test context — create a fresh unregistered instance
        svc = McpClientService(server_configs)
        await svc.connect_all()
        return svc

    # Fast path: already initialized (no lock needed — read-only check)
    if app.bot_data.get('mcp_service') is not None:
        app.bot_data['mcp_last_used'] = time.monotonic()
        return app.bot_data['mcp_service']

    # Slow path: acquire lock, double-check, then initialize
    if 'mcp_init_lock' not in app.bot_data:
        app.bot_data['mcp_init_lock'] = asyncio.Lock()

    async with app.bot_data['mcp_init_lock']:
        # Double-check after acquiring the lock
        if app.bot_data.get('mcp_service') is not None:
            app.bot_data['mcp_last_used'] = time.monotonic()
            return app.bot_data['mcp_service']

        logger.info("Initializing MCP service (connect_all)...")
        svc = McpClientService(server_configs)
        await svc.connect_all()
        app.bot_data['mcp_service'] = svc
        app.bot_data['mcp_last_used'] = time.monotonic()
        logger.info("MCP service initialized and registered in bot_data.")
        return svc


async def get_or_init_skill_service(app, enable_skills: bool):
    """Return the shared SkillRegistryService, initializing it exactly once.

    Args:
        app:           The PTB Application instance, or None in QA/test contexts.
        enable_skills: Whether skill registry is enabled for this chat/session.

    Returns:
        A loaded SkillRegistryService, or None if enable_skills is False.
    """
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
    """Refresh the MCP last-used timestamp just before a tool call executes.

    Prevents the idle watchdog from deciding the service is idle while a long-running
    request still holds a live reference and is actively executing tools.
    """
    if app is not None and app.bot_data.get('mcp_service') is not None:
        app.bot_data['mcp_last_used'] = time.monotonic()


async def shutdown_mcp_service_if_idle(app, idle_seconds: int = _DEFAULT_MCP_IDLE_SECONDS) -> bool:
    """Shut down MCP subprocesses if no request has used them within idle_seconds.

    Called by the APScheduler watchdog every 5 minutes. On shutdown, the service
    reference is cleared so the next get_or_init_mcp_service() call reconnects
    transparently. Returns True if shutdown occurred, False otherwise.

    Race safety: bot_data['mcp_service'] is set to None BEFORE cleanup_all() is
    awaited, so any concurrent fast-path caller that races past the None check will
    find None and fall through to the slow path (which blocks on mcp_init_lock
    until cleanup finishes, then re-initializes cleanly).
    """
    if app is None:
        return False

    if app.bot_data.get('mcp_service') is None:
        return False  # Already shut down or never initialized

    last_used = app.bot_data.get('mcp_last_used', 0)
    if time.monotonic() - last_used < idle_seconds:
        return False  # Still within the active window

    if 'mcp_init_lock' not in app.bot_data:
        app.bot_data['mcp_init_lock'] = asyncio.Lock()

    async with app.bot_data['mcp_init_lock']:
        # Re-check inside the lock — a concurrent request may have updated last_used
        mcp_service = app.bot_data.get('mcp_service')
        if mcp_service is None:
            return False

        last_used = app.bot_data.get('mcp_last_used', 0)
        if time.monotonic() - last_used < idle_seconds:
            return False

        elapsed_min = (time.monotonic() - last_used) / 60
        logger.info(
            f"MCP service idle for {elapsed_min:.0f} min — shutting down subprocesses "
            f"to free memory (~150-200 MB). Will reconnect on next MCP request."
        )
        # Null out the reference first so concurrent fast-path callers see None
        app.bot_data['mcp_service'] = None
        try:
            await mcp_service.cleanup_all()
        except Exception as e:
            logger.warning(f"Non-fatal error during idle MCP subprocess cleanup: {e}")
        return True

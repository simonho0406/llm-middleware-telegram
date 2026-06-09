import os
import json
import logging
import subprocess

from hooks.security_policy import (
    BLOCKED_TOOL_NAMES as _POLICY_BLOCKED_TOOL_NAMES,
    BLOCKED_PATH_PATTERNS as _POLICY_BLOCKED_PATH_PATTERNS,
    BLOCKED_COMMANDS as _POLICY_BLOCKED_COMMANDS,
)

logger = logging.getLogger(__name__)


class HookScriptError(PermissionError):
    """Hook script failed to execute (syntax error, missing interpreter, crash).

    Subclasses PermissionError to preserve fail-closed behavior at all call sites,
    but allows log monitoring and tests to distinguish a configuration problem from
    a deliberate security denial (CalledProcessError with non-zero exit).
    """
    pass


class HookRunner:
    def __init__(self, hooks_dir: str = 'hooks'):
        self.hooks_dir = hooks_dir
        # Ensure directory exists but do not enforce scripts must exist
        if not os.path.isdir(self.hooks_dir):
            os.makedirs(self.hooks_dir, exist_ok=True)

    # Blocklists imported from hooks/security_policy.py — single source of truth
    # shared with hooks/pre_tool_use.py so the fallback and subprocess paths
    # always enforce the same rules.
    _BLOCKED_PATH_PATTERNS = _POLICY_BLOCKED_PATH_PATTERNS
    _BLOCKED_COMMANDS = _POLICY_BLOCKED_COMMANDS
    _BLOCKED_TOOL_NAMES = _POLICY_BLOCKED_TOOL_NAMES

    def run_pre_tool_use(self, tool_name: str, payload: dict) -> None:
        """
        Executes pre-tool-use hooks. Accepts any tool_name (MCP tools namespaced
        with '__', skills namespaced with 'skill_', etc.).

        Priority:
        1. If hooks/pre_tool_use.py exists, execute it via subprocess.
        2. Otherwise, run built-in Python fallback safety checks.

        Raises PermissionError if the tool invocation is denied.
        Raises HookScriptError (subclass of PermissionError) if the hook script
        itself fails to run (syntax error, missing interpreter, etc.).
        """
        hook_path = os.path.join(self.hooks_dir, 'pre_tool_use.py')

        if os.path.exists(hook_path):
            self._run_hook_script(hook_path, tool_name, payload)
        else:
            self._run_fallback_validation(tool_name, payload)

    def _run_fallback_validation(self, tool_name: str, payload: dict) -> None:
        """
        Built-in safety checks applied when no external hook script exists.
        Blocks path traversals, unauthorized system paths, and destructive commands.
        """
        # Tool-name-level block: deny regardless of arguments (e.g. sqlite write tools).
        if tool_name in self._BLOCKED_TOOL_NAMES:
            msg = f"Access Denied: Tool '{tool_name}' is blocked by security policy."
            logger.warning(f"Fallback hook validation: {msg}")
            raise PermissionError(msg)

        # Path/command checks are only meaningful for local-execution tools (no `__` namespace).
        # MCP tools (e.g. `notion-workspace__API-search`) pass args to external servers via
        # structured API — blocking "update " or "/usr" in a search query causes false positives.
        if "__" in tool_name:
            return

        args_str = str(payload.get('arguments', ''))

        for pattern in self._BLOCKED_PATH_PATTERNS:
            if pattern in args_str:
                msg = f"Access Denied: Blocked pattern '{pattern}' detected in arguments for tool '{tool_name}'."
                logger.warning(f"Fallback hook validation: {msg}")
                raise PermissionError(msg)

        args_lower = args_str.lower()
        for cmd in self._BLOCKED_COMMANDS:
            if cmd in args_lower:
                msg = f"Access Denied: Blocked command '{cmd.strip()}' detected in arguments for tool '{tool_name}'."
                logger.warning(f"Fallback hook validation: {msg}")
                raise PermissionError(msg)

    def _run_hook_script(self, hook_path: str, tool_name: str, payload: dict) -> None:
        """Executes the external pre_tool_use.py hook script via subprocess."""
        payload_json = json.dumps({
            'tool_name': tool_name,
            'payload': payload
        })

        logger.info(f"Running pre_tool_use hook for {tool_name}")

        try:
            subprocess.run(
                ["python3", hook_path],
                input=payload_json,
                text=True,
                capture_output=True,
                check=True,
                timeout=10
            )
        except subprocess.CalledProcessError as e:
            # Non-zero exit = deliberate security denial by the hook policy
            error_message = e.stderr.strip() or e.stdout.strip() or f"Hook exited with code {e.returncode}"
            logger.warning(f"pre_tool_use hook denied access for {tool_name}: {error_message}")
            raise PermissionError(error_message)
        except subprocess.TimeoutExpired:
            logger.warning(f"pre_tool_use hook timed out for {tool_name}")
            raise PermissionError("Hook validation timed out.")
        except Exception as e:
            # Script could not be executed at all (syntax error, missing interpreter, etc.)
            # Raise HookScriptError so operators can distinguish config problems from security events.
            logger.critical(
                f"pre_tool_use hook script failed to execute for '{tool_name}': {e}. "
                f"All tool use is blocked until the hook script is fixed."
            )
            raise HookScriptError(f"Hook script execution error: {e}")


hook_runner = HookRunner()

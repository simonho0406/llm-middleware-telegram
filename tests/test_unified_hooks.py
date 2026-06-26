import os
import sys
import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock
from utils.hooks import HookRunner
from hooks.security_policy import BLOCKED_TOOL_NAMES

@pytest.fixture
def temp_hooks_dir(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    return str(hooks_dir)

def test_fallback_validation_benign(temp_hooks_dir):
    """If no script exists, benign payloads should pass through cleanly."""
    runner = HookRunner(hooks_dir=temp_hooks_dir)
    payload = {
        'arguments': {
            'target_directory': 'data/projects',
            'query': 'SELECT * FROM users'
        }
    }
    # Should not raise any error
    runner.run_pre_tool_use('sqlite_query', payload)

@pytest.mark.parametrize("bad_arg", [
    "../etc/passwd",
    "/etc/shadow",
    "rm -rf /",
    "mkfs.ext4 /dev/sda",
    "../../secrets.txt",
    "/bin/bash",
])
def test_fallback_validation_blocked(temp_hooks_dir, bad_arg):
    """If no script exists, fallback should block path traversals and dangerous commands."""
    runner = HookRunner(hooks_dir=temp_hooks_dir)
    payload = {
        'arguments': {
            'path': bad_arg
        }
    }
    with pytest.raises(PermissionError) as excinfo:
        runner.run_pre_tool_use('write_file', payload)
    
    assert "Access Denied" in str(excinfo.value)

def test_script_execution_success(temp_hooks_dir):
    """If pre_tool_use.py exists and returns 0, execution is allowed."""
    runner = HookRunner(hooks_dir=temp_hooks_dir)
    
    script_path = os.path.join(temp_hooks_dir, "pre_tool_use.py")
    with open(script_path, "w") as f:
        f.write("""import sys
import json
# Accept everything and exit 0
sys.exit(0)
""")
        
    payload = {
        'arguments': {
            'target_dir': 'safe_path'
        }
    }
    # Should run and not raise error
    runner.run_pre_tool_use('code_review', payload)

def test_script_execution_denied(temp_hooks_dir):
    """If pre_tool_use.py returns non-zero, it raises PermissionError with stderr."""
    runner = HookRunner(hooks_dir=temp_hooks_dir)
    
    script_path = os.path.join(temp_hooks_dir, "pre_tool_use.py")
    with open(script_path, "w") as f:
        f.write("""import sys
import json
print("Blocked by security policy!", file=sys.stderr)
sys.exit(1)
""")
        
    payload = {
        'arguments': {
            'target_dir': 'safe_path'
        }
    }
    with pytest.raises(PermissionError) as excinfo:
        runner.run_pre_tool_use('code_review', payload)
        
    assert "Blocked by security policy!" in str(excinfo.value)

def test_script_execution_timeout(temp_hooks_dir):
    """If the script hangs, it raises PermissionError on timeout."""
    runner = HookRunner(hooks_dir=temp_hooks_dir)
    
    script_path = os.path.join(temp_hooks_dir, "pre_tool_use.py")
    with open(script_path, "w") as f:
        f.write("""import sys
import time
time.sleep(15)
""")
        
    payload = {
        'arguments': {
            'target_dir': 'safe_path'
        }
    }
    
    # Mock subprocess.run to raise subprocess.TimeoutExpired so we don't actually sleep 15s.
    import subprocess
    with patch('subprocess.run') as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["python3", script_path], timeout=1)
        with pytest.raises(PermissionError) as excinfo:
            runner.run_pre_tool_use('code_review', payload)
        assert "timed out" in str(excinfo.value)


# ── Panel tool-call authority gate (real _execute_panel_tool_calls) ──────────────
# These drive the actual production code in bot.handlers.panel_workflow rather than a
# re-implementation, so a regression in the gate would fail these tests.

def _panel_args():
    """Common kwargs for _execute_panel_tool_calls, sans the per-test ones."""
    return dict(
        skill_service=None,
        tool_result_cache={},
        user_prompt="q",
        dossier_token_budget=500,
        context=MagicMock(),
    )


@pytest.mark.asyncio
async def test_panel_empty_authority_denies_all():
    """Empty panel_execution_tool_names (frozenset()) must deny ALL tools (fail-closed)
    and never reach mcp_service.execute_tool."""
    from bot.handlers import panel_workflow

    mcp_service = AsyncMock()
    kwargs = _panel_args()
    cache = kwargs["tool_result_cache"]
    with patch.object(panel_workflow, "distill_tool_result",
                      new=AsyncMock(side_effect=lambda result, **kw: result)):
        parts = await panel_workflow._execute_panel_tool_calls(
            tool_calls=[{"name": "sqlite-tools__read_query", "arguments": {}}],
            mcp_service=mcp_service,
            panel_execution_tool_names=frozenset(),
            **kwargs,
        )

    assert "[Denied: Panel tool authority set is empty" in parts[0]
    mcp_service.execute_tool.assert_not_awaited()
    assert cache == {}, "denials must not be cached"


@pytest.mark.asyncio
async def test_panel_unauthorized_tool_denied():
    """A tool absent from a non-empty authority set is denied without execution."""
    from bot.handlers import panel_workflow

    mcp_service = AsyncMock()
    kwargs = _panel_args()
    with patch.object(panel_workflow, "distill_tool_result",
                      new=AsyncMock(side_effect=lambda result, **kw: result)):
        parts = await panel_workflow._execute_panel_tool_calls(
            tool_calls=[{"name": "sqlite-tools__read_query", "arguments": {}}],
            mcp_service=mcp_service,
            panel_execution_tool_names=frozenset({"tavily-search__search"}),
            **kwargs,
        )

    assert "[Denied:" in parts[0] and "not authorised" in parts[0]
    mcp_service.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_authorized_tool_denied_by_hook():
    """An authorized tool still passes through Gate 2 (the security hook); a hook
    PermissionError blocks execution."""
    from bot.handlers import panel_workflow

    mcp_service = AsyncMock()
    kwargs = _panel_args()
    cache = kwargs["tool_result_cache"]
    with patch.object(panel_workflow, "distill_tool_result",
                      new=AsyncMock(side_effect=lambda result, **kw: result)), \
         patch.object(panel_workflow.hook_runner, "run_pre_tool_use",
                      side_effect=PermissionError("blocked by policy")):
        parts = await panel_workflow._execute_panel_tool_calls(
            tool_calls=[{"name": "sqlite-tools__read_query", "arguments": {}}],
            mcp_service=mcp_service,
            panel_execution_tool_names=frozenset({"sqlite-tools__read_query"}),
            **kwargs,
        )

    assert "[Denied by security hook" in parts[0]
    mcp_service.execute_tool.assert_not_awaited()
    assert cache == {}, "hook denials must not be cached"


@pytest.mark.asyncio
async def test_panel_authorized_tool_executes_and_caches():
    """Authorized tool + passing hook → executed once and the genuine result is cached."""
    from bot.handlers import panel_workflow

    mcp_service = AsyncMock()
    mcp_service.execute_tool = AsyncMock(return_value="ROWS: 42")
    kwargs = _panel_args()
    cache = kwargs["tool_result_cache"]
    with patch.object(panel_workflow, "distill_tool_result",
                      new=AsyncMock(side_effect=lambda result, **kw: result)), \
         patch.object(panel_workflow, "touch_mcp_last_used", MagicMock()), \
         patch.object(panel_workflow.hook_runner, "run_pre_tool_use", MagicMock()):
        parts = await panel_workflow._execute_panel_tool_calls(
            tool_calls=[{"name": "sqlite-tools__read_query", "arguments": {}}],
            mcp_service=mcp_service,
            panel_execution_tool_names=frozenset({"sqlite-tools__read_query"}),
            **kwargs,
        )

    mcp_service.execute_tool.assert_awaited_once_with("sqlite-tools", "read_query", {})
    assert "ROWS: 42" in parts[0]
    # The genuine result (not a denial/error) is what gets cached for cross-call dedupe.
    assert "ROWS: 42" in cache.values()


def test_hook_script_error_raises_hook_script_error(temp_hooks_dir):
    """When the hook subprocess cannot be launched at all (e.g. python3 not found,
    OSError from the OS), HookScriptError must be raised — NOT a generic PermissionError.
    This lets operators distinguish a configuration failure from a deliberate security denial.

    Note: a script that runs but exits non-zero (e.g. syntax error, policy denial) raises
    plain PermissionError via CalledProcessError — that is intentional fail-closed behavior.
    HookScriptError covers the 'subprocess could not start' case.
    """
    from utils.hooks import HookRunner, HookScriptError
    import subprocess

    # Create a valid script so the hook_path exists (triggering the subprocess path)
    valid_script = os.path.join(temp_hooks_dir, 'pre_tool_use.py')
    with open(valid_script, 'w') as f:
        f.write("import sys; sys.exit(0)\n")

    runner = HookRunner(hooks_dir=temp_hooks_dir)

    # Simulate OS-level failure to launch the subprocess (e.g. python3 not found)
    with patch('subprocess.run', side_effect=OSError("python3 not found")):
        with pytest.raises(HookScriptError):
            runner.run_pre_tool_use('any_tool', {'arguments': {}})

    # HookScriptError must be a subclass of PermissionError (fail-closed invariant)
    assert issubclass(HookScriptError, PermissionError)


# ── Real subprocess hook + single-source-of-truth parity ─────────────────────────
# The tests above use synthetic temp scripts. These exercise the REAL hooks/pre_tool_use.py
# against the REAL blocklist, so the deployed subprocess path is actually validated.

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_HOOKS_DIR = os.path.join(_REPO_ROOT, "hooks")


@pytest.mark.parametrize("blocked_tool", sorted(BLOCKED_TOOL_NAMES))
def test_real_pre_tool_use_blocks_each_blocked_tool(blocked_tool):
    """The real hooks/pre_tool_use.py subprocess must deny every BLOCKED_TOOL_NAMES entry,
    regardless of arguments."""
    runner = HookRunner(hooks_dir=_REAL_HOOKS_DIR)
    with pytest.raises(PermissionError) as excinfo:
        runner.run_pre_tool_use(blocked_tool, {"arguments": {}})
    assert "blocked by security policy" in str(excinfo.value).lower()


def test_real_pre_tool_use_allows_benign_read_tool():
    """A read-only MCP tool not in the blocklist passes the real subprocess hook
    (its '__' namespace skips the path/command substring scans)."""
    runner = HookRunner(hooks_dir=_REAL_HOOKS_DIR)
    # Should not raise.
    runner.run_pre_tool_use("sqlite-tools__read_query", {"arguments": {"query": "SELECT 1"}})


def test_real_pre_tool_use_does_not_substring_scan_mcp_args():
    """MCP tool args (namespaced with '__') are NOT substring-scanned, so a legitimate
    query containing 'update ' or '/usr' is allowed — parity with the Python fallback's
    '__' skip rule."""
    runner = HookRunner(hooks_dir=_REAL_HOOKS_DIR)
    runner.run_pre_tool_use(
        "notion-workspace__API-search",
        {"arguments": {"query": "how to update the /usr layout"}},
    )


def test_blocklist_single_source_of_truth():
    """utils/hooks.py and hooks/pre_tool_use.py must share ONE blocklist object so the
    fallback and subprocess paths can never diverge."""
    from hooks import security_policy
    assert HookRunner._BLOCKED_TOOL_NAMES is security_policy.BLOCKED_TOOL_NAMES
    assert HookRunner._BLOCKED_PATH_PATTERNS is security_policy.BLOCKED_PATH_PATTERNS
    assert HookRunner._BLOCKED_COMMANDS is security_policy.BLOCKED_COMMANDS

import os
import json
import logging
import subprocess

logger = logging.getLogger(__name__)

class HookRunner:
    def __init__(self, hooks_dir: str = 'hooks'):
        self.hooks_dir = hooks_dir
        # Ensure directory exists but do not enforce scripts must exist
        if not os.path.isdir(self.hooks_dir):
            os.makedirs(self.hooks_dir, exist_ok=True)

    def run_pre_tool_use(self, tool_name: str, payload: dict) -> None:
        """
        Executes pre-tool-use hooks. If any hook returns a non-zero exit status,
        it raises a PermissionError with the hook's output.
        """
        hook_path = os.path.join(self.hooks_dir, 'pre_tool_use.py')
        if not os.path.exists(hook_path):
            return

        payload_json = json.dumps({
            'tool_name': tool_name,
            'payload': payload
        })

        logger.info(f"Running pre_tool_use hook for {tool_name}")
        
        try:
            # We run the python script, passing the payload via stdin
            result = subprocess.run(
                ["python3", hook_path],
                input=payload_json,
                text=True,
                capture_output=True,
                check=True,
                timeout=10
            )
        except subprocess.CalledProcessError as e:
            error_message = e.stderr.strip() or e.stdout.strip() or f"Hook exited with code {e.returncode}"
            logger.warning(f"pre_tool_use hook denied access for {tool_name}: {error_message}")
            raise PermissionError(error_message)
        except subprocess.TimeoutExpired:
            logger.warning(f"pre_tool_use hook timed out for {tool_name}")
            raise PermissionError("Hook validation timed out.")
        except Exception as e:
            logger.error(f"Failed to execute pre_tool_use hook: {e}")
            raise PermissionError(f"Internal hook execution error: {e}")

hook_runner = HookRunner()

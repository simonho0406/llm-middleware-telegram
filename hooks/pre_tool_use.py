import sys
import os
import json

# Add the project root to the path so we can import from hooks.security_policy.
# This script runs as a subprocess from the project root directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hooks.security_policy import BLOCKED_TOOL_NAMES, BLOCKED_PATH_PATTERNS, BLOCKED_COMMANDS


def main():
    try:
        data = json.load(sys.stdin)
        tool_name = data.get('tool_name', '')
        payload = data.get('payload', {})

        # Gate 1: tool-name-level block (write paths, regardless of arguments)
        if tool_name in BLOCKED_TOOL_NAMES:
            print(f"Access Denied: Tool '{tool_name}' is blocked by security policy.", file=sys.stderr)
            sys.exit(1)

        # Gates 2/3 are only meaningful for local-execution tools (no `__` namespace).
        # MCP tools pass args to external servers via structured API — substring checks
        # on those args would block legitimate queries containing "update", "/usr", etc.
        if '__' not in tool_name:
            args_str = str(payload.get('arguments', ''))

            # Gate 2: path traversal / unauthorized system paths
            for pattern in BLOCKED_PATH_PATTERNS:
                if pattern in args_str:
                    print(f"Access Denied: Blocked pattern '{pattern}' detected for tool '{tool_name}'.", file=sys.stderr)
                    sys.exit(1)

            # Gate 3: destructive shell/SQL commands
            args_lower = args_str.lower()
            for cmd in BLOCKED_COMMANDS:
                if cmd in args_lower:
                    print(f"Access Denied: Blocked command '{cmd.strip()}' detected for tool '{tool_name}'.", file=sys.stderr)
                    sys.exit(1)

        sys.exit(0)

    except Exception as e:
        print(f"Hook processing error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

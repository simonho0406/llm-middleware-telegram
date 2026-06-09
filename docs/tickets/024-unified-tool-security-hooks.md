# Ticket 024: Unified Tool Security Hooks

**Priority:** P1
**Component:** Security / Hooks Subsystem
**Status:** ✅ Implemented & Verified
**Prerequisites:** None

---

## 1. Description
Extend the pluggable security hooks subsystem in `hooks/` and `utils/hooks.py` to validate all incoming tool executions (both standard MCP tools and local Skill execution commands) prior to execution, ensuring safety constraints and user policies are strictly enforced.

## 2. Architectural Pillars (Immutable)
*   **Pillar D (Configuration-Driven)**: Hook behavior and allowed tool whitelists must be defined dynamically.
*   **Pillar B (Centralized, Safe Rendering)**: Standardize error reporting. If a hook raises a `PermissionError`, it must be returned as a structured system response back to the LLM (so the LLM knows it was denied and can adapt or inform the user safely) rather than crashing the thread.

## 3. Proposed Changes

### 3.1 Upgrade HookRunner
Modify [utils/hooks.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/utils/hooks.py):
*   Extend `run_pre_tool_use(tool_name, payload)` to be generic:
    -   Accept any `tool_name` (e.g. `sqlite_query`, `google_search`, `skill_code_review`).
    -   Pass the complete payload containing arguments and context parameters (e.g. `user_id`, `chat_id`).
*   Ensure that if a subprocess-based hook script `hooks/pre_tool_use.py` exists, it is called, passing the serialized JSON payload via stdin.
*   Implement standard fallback checks in Python if the hook script is missing (e.g., blocking forbidden system commands like `rm`, `mkfs`, or unauthorized file paths).

### 3.2 Implement a Sample Security Hook
Create a template [hooks/pre_tool_use.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/hooks/pre_tool_use.py):
*   Read JSON payload from `sys.stdin`.
*   Implement simple validation rule (e.g. if the tool arguments contain forbidden characters or path traversals like `../`, exit with a non-zero status and print an informative error message on stderr).
*   Example content:
    ```python
    import sys
    import json

    def main():
        try:
            data = json.load(sys.stdin)
            tool_name = data.get('tool_name')
            payload = data.get('payload', {})
            
            # Simple check: Block path traversals in arguments
            args_str = str(payload.get('arguments', ''))
            if '..' in args_str or '/etc' in args_str:
                print("Access Denied: Path traversal or unauthorized system path detected.", file=sys.stderr)
                sys.exit(1)
                
            sys.exit(0)
        except Exception as e:
            print(f"Hook processing error: {e}", file=sys.stderr)
            sys.exit(1)

    if __name__ == '__main__':
        main()
    ```

## 4. Verification & Testing
*   **Test Case 1 (Security Block)**: Mock a tool invocation with arguments containing `../etc/passwd`. Run the hook runner. Assert it raises `PermissionError` containing the exact error message.
*   **Test Case 2 (Pass-through)**: Mock a benign tool invocation. Assert the hook runner exits successfully without raising any exceptions.
*   **Test Case 3 (Timeout Resiliency)**: Verify that if a hook hangs, `HookRunner` catches it after a strict timeout (e.g., 10 seconds), raises a clean timeout exception, and rejects the tool execution.

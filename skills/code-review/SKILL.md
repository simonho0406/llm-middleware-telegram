---
name: "code-review"
description: "Review codebase changes for styling, architectural compliance, and potential bugs."
parameters:
  type: "object"
  properties:
    target_directory:
      type: "string"
      description: "Path to directory or file to review."
  required: ["target_directory"]
---
# Code Review Playbook
Perform these instructions precisely:
1. List files in the target directory to verify target files exist.
2. Carefully inspect the implementation details of any modified or new Python files.
3. Compare the codebase implementation against our Architectural Pillars:
   - **Pillar A**: Stateless, Class-Based Services (instantiable, no global mutable state).
   - **Pillar B**: Centralized, Safe Rendering (always use `messaging.send_safe_message`).
   - **Pillar C**: Robust State Management (tracked background tasks, proper cancel cleanup).
   - **Pillar D**: Configuration-Driven (no hardcoded model names or provider logic).
4. Identify any potential bugs (NameErrors, silent exceptions, resource leaks).
5. Output a structured report with severity levels (Critical/High/Medium/Low) and explicit recommendations.

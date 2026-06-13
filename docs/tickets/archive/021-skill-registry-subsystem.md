# Ticket 021: Markdown Skill Registry Subsystem

**Priority:** P1
**Component:** Services / Extensibility
**Status:** ✅ Implemented & Verified
**Prerequisites:** None

---

## 1. Description
Build a local custom "Skill Playbook" registry subsystem inspired by OpenClaw and Claude Code. Skills are declared in Markdown files with YAML frontmatter. The system registers the skill's name and description as an available tool to the LLM, and deferredly loads the full markdown instructions only when the LLM chooses to execute that skill.

## 2. Architectural Pillars (Immutable)
*   **Pillar A (Stateless Service)**: `SkillRegistryService` must be a self-contained service class initialized with a directory path (default: `skills/`), caching loaded metadata and instructions in-memory on startup.
*   **Pillar D (Configuration-Driven)**: Active skills and directories must be configured via `config.yaml` or sqlite user settings.

## 3. Proposed Changes

### 3.1 Define Skill Format
Create a template directory structure: `skills/`
Every skill consists of `skills/<skill-name>/SKILL.md`. Example structure:
```markdown
---
name: "code-review"
description: "Review codebase changes for styling and pillar compliance."
parameters:
  type: "object"
  properties:
    target_directory:
      type: "string"
      description: "Path to directory to review."
  required: ["target_directory"]
---
# Code Review Playbook
Perform these instructions precisely:
1. List the files in the directory.
2. Read the implementation details of key python files.
3. Compare them against our Architectural Pillars and write a detailed analysis.
```

### 3.2 Create the SkillRegistryService Class
Create [services/skill_service.py](file:///Users/simonho/Library/CloudStorage/OneDrive-Personal/Files/projects/LLM_middleware/llm-middleware-telegram/services/skill_service.py):
*   Class signature:
    ```python
    import os
    import yaml
    from typing import Dict, List, Any

    class SkillRegistryService:
        def __init__(self, skills_dir: str = "skills"):
            self.skills_dir = skills_dir
            self.skills: Dict[str, Dict[str, Any]] = {}  # skill_name -> parsed dictionary

        def load_skills(self):
            """
            Scans self.skills_dir for directories containing SKILL.md.
            Parses YAML frontmatter, extracts parameters and descriptions,
            and stores metadata and natural-language playbook separately.
            """
            pass

        def get_skills_as_tools(self) -> List[Dict[str, Any]]:
            """
            Formats each skill's metadata into standard OpenAI Tool Calling schema.
            The description should clearly invite the LLM to call the skill.
            """
            pass

        def get_skill_playbook(self, skill_name: str) -> str:
            """Retrieves the full Markdown body of the skill (deferred execution context)."""
            pass
    ```

## 4. Verification & Testing
*   **Test Case 1 (Parser)**: Set up a temporary skills directory with two valid and one malformed `SKILL.md`. Assert that `load_skills()` registers the valid skills, parses parameters correctly, and logs/skips the malformed skill safely.
*   **Test Case 2 (Tool Output)**: Verify `get_skills_as_tools()` maps YAML types and required parameters perfectly to standard OpenAI JSON Schema function specs.

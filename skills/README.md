# Skills

Skills are natural-language **playbooks** exposed to the chat model as
`skill_<name>` tools. When the model calls one, `SkillRegistryService` returns
the playbook text (the body below the frontmatter) — it does **not** execute
code. So a skill is only useful if the model can actually carry it out with the
tools it has in the Telegram runtime (web search via `<search>`, MCP tools, and
its own reasoning). Skills that assume filesystem/codebase access do **not** work
here — the chat model has no such tools. (The former `code-review` skill was
removed for exactly this reason.)

## Adding a skill

Create `skills/<name>/SKILL.md` with YAML frontmatter + a playbook body:

```markdown
---
name: "summarize-thread"
description: "Produce a structured summary of the user's current conversation thread."
parameters:
  type: "object"
  properties:
    focus:
      type: "string"
      description: "Optional topic to focus the summary on."
  required: []
---
# Summarize Thread Playbook
1. ...
2. ...
```

- `name` becomes the tool name `skill_<name>`.
- `description` is what the model sees when deciding whether to use it — make it
  precise; the model routes on this text.
- `parameters` is a JSON-Schema object for the tool's arguments.
- The body (everything after the second `---`) is the playbook returned verbatim.

Skills are auto-discovered at startup; no code changes needed. Enable/disable per
chat via the `enable_skills` user setting.

# Skills

Skills give stcode reusable instructions for tasks such as reviewing code,
writing release notes, or following a project workflow.

## Install a skill

Place a skill folder in one of these locations:

| Location | Available to |
| --- | --- |
| `<project>/.agents/skills/` | That project |
| `~/.agents/skills/` | Your projects |
| Directories in `STCODE_SKILLS_PATH` | Sessions started with that environment setting |

`STCODE_SKILLS_PATH` is a colon-separated list on Linux and macOS. Open a new
session after installing a skill, then use `/skills` to check that it was found.

## Use a skill

Discovered skills appear in the `/` command picker. Select one or type its name
with a request:

```text
/release-notes Summarize the changes since the last release.
```

You can also ask for it in ordinary language. Built-in commands keep their own
meaning if a skill has the same name.

## Create a skill

Create `.agents/skills/release-notes/SKILL.md`:

```markdown
---
name: release-notes
description: Write release notes from a range of commits. Use when asked to summarize what shipped.
---

# Release notes

Read the commits in the requested range. Group changes by feature, fix, and
maintenance. Write a short user-facing description for each change and link
relevant issues when available.
```

Include a name and a description explaining when to use it. Add templates or
reference files alongside `SKILL.md` and refer to them from its instructions.

For executable integrations, connect an [MCP server](mcp.md) or use a
[custom Python tool](../sdk/custom-tools.md).

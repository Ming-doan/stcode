# Skills

A skill is a folder with a `SKILL.md`: YAML frontmatter naming it and saying when it
applies, then markdown instructions, then whatever those instructions reference.

```
~/.agents/skills/release-notes/
  SKILL.md          <- frontmatter + instructions
  template.md       <- read on demand, from inside the instructions
```

```markdown
---
name: release-notes
description: Write release notes from a range of commits. Use when asked to summarise what shipped.
---

# Release notes

Read `git log <from>..<to> --oneline`, group by area, and write one line per change in
the imperative mood.
```

## Progressive disclosure is the whole design

Discovery reads **only the frontmatter**. The body loads when the agent calls
`skill("release-notes")`. Anything the body points at loads only if the agent follows
the pointer.

Nine skills cost about 400 tokens of catalogue instead of about 90,000 tokens of
instructions. That ratio is the reason the format exists, and it is why a `SKILL.md`
larger than 256 KB is refused: long material belongs in a file the instructions point
at, so it is read only when it is actually needed.

## Where they are found

Searched in order, nearest first:

1. `$STCODE_SKILLS_PATH` — colon-separated, searched before everything else
2. `<project>/.agents/skills/`
3. `~/.agents/skills/`

The format and the `~/.agents/skills` path are the cross-agent convention, so skills
written for another agent work here unchanged.

## Writing one

The `description` is the only thing the model sees until it loads the skill, so it has
one job: **let the model decide whether this applies**. Say when to use it, not what it
is.

| | |
| --- | --- |
| Bad | `Helpers for release management.` |
| Good | `Write release notes from a range of commits. Use when asked to summarise what shipped.` |

A skill with no `description` in its frontmatter is skipped, and the problem is
reported rather than swallowed — a skill that silently does not exist is worse than one
that fails.

## What a skill is not

It is not code and it is not a tool. It is a procedure written for a model, loaded when
relevant. If it needs to *do* something the built-in tools cannot, that is a
[tool](../sdk/custom-tools.md) or an [MCP server](mcp.md), not a skill.

# 0001 — Load tool definitions on demand

**Status: rejected** as a per-turn mechanism. A narrower version is recommended instead.

## Context

Tool definitions sit in the request's **cached prefix**: name, description and JSON
Schema for every advertised tool, sent on every single request for the life of the
session.

That is the same observation that produced [MCP as code](../guide/mcp.md), which moved
three servers' twelve tools from 9,869 chars per turn to zero. The obvious next
question is whether the *built-in* tools deserve the same treatment — a `load_tool`
call, as some agents now ship, that injects a tool's schema into the turn when the
model asks for it.

### The measurement

Measured on this repository, `full-auto`, no MCP, no skills:

| | chars | ≈ tokens |
| --- | ---: | ---: |
| 13 built-in tool definitions | **14,503** | 3,625 |
| System prompt | 6,111 | 1,527 |
| **Prefix total** | **20,614** | **5,153** |

The five most expensive tools — `grep`, `bash`, `todo_write`, `web_search`,
`ask_user_question` — are 8,058 chars between them, 56% of the total.

So the pot is real, and bigger than the MCP saving already banked. Reproduce it with
`harness.tool_definitions()` and `json.dumps`.

## The options

### A. Do nothing

13 tools, 14,503 chars, every turn.

### B. Tool search — inject on demand

Advertise a small core (`read`, `grep`, `bash`, `tool_search`) plus a search tool. The
model calls `tool_search("web")`, and the matching definitions are added to the tool
list for the rest of the conversation.

### C. Static selection at session start

Choose the tool set once, before the first request, from the task — either by config
(`[agent] tools`, which already exists) or by one cheap model call.

## The decision

**B is rejected. C is the recommendation, and most of it is already built.**

### Why B loses

The headline number is misleading, and the direction of the error matters.

14,503 chars is what the definitions cost **uncached**. With prompt caching they are
written once at 1.25× and read thereafter at 0.1×, so their true marginal cost from
turn two onward is about **363 token-equivalents per turn**, not 3,625. Caching has
already taken a 10× discount on exactly the thing tool search proposes to optimise.

Worse, the prefix is cached **in order** — tools, then system, then messages. Changing
the tools block invalidates everything after it, *including the conversation history*.
And by mid-session the history dwarfs both of the other two.

Take a coding session ten turns in, with ~30,000 tokens of history:

| | cost of the turn's prefix |
| --- | ---: |
| Cache read, nothing changed | 30,000 × 0.1 = **3,000** |
| Cache write, after one injection | 30,000 × 1.25 = **37,500** |

One injection costs about **34,500 token-equivalents**. The saving it is buying is
~363 per turn. It pays for itself after roughly **95 turns without a second
injection** — and a mechanism whose whole premise is that the model discovers tools as
it needs them will not go 95 turns without discovering another one.

Tool search is an optimisation that, in an agent loop with prefix caching, is
**net negative** in every scenario we can construct.

### Why C wins

Choosing the tool set before the first request keeps the prefix byte-stable for the
whole session. It captures the same saving with none of the invalidation, because
nothing changes after the cache is written.

And the mechanism exists:

```toml
[agent]
tools = ["read", "grep", "glob", "ls", "todo_write"]
exclude_tools = ["web_search", "repl"]
```

What is *not* built is choosing that list automatically — one `difficulty = "low"` call
at session start, deciding which tools this task plausibly needs. That is a small,
cache-safe feature, and it is the version of this idea worth building.

## Consequences

- The built-in set stays at 14,503 chars in the prefix, discounted ~10× by caching.
- Sessions that genuinely do not need a tool can already drop it, per deployment, with
  one line of config.
- `expose = "tools"` MCP mode stays the one place where the prefix can grow without
  bound, and stays not the default.

## What would change this

Any one of these reopens it:

1. **A provider without prefix caching** becomes a primary target. The 10× discount is
   the entire argument; without it the arithmetic inverts.
2. **The tool set grows past the history.** With 50+ MCP tools advertised in `tools`
   mode, the tools block can exceed the conversation for the first several turns, and
   a *pre-first-request* narrowing — still option C, not B — becomes worth automating.
3. **Providers expose a cache breakpoint after the tools block that survives a tools
   change.** If a definition could be appended without invalidating the history, the
   34,500 becomes the cost of the tools block alone and B becomes arguable.
4. **The measurement changes.** Rerun it before citing it; this one is from a 13-tool
   build with no MCP servers attached.

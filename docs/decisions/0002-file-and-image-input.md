# 0002 — Files and images as model input

**Status: accepted, not yet built.** This records the shape agreed before
implementation, so the work can start from a decision rather than from a blank page.

## Context

Today the unified message format carries three block types — `TextBlock`,
`ToolUseBlock`, `ToolResultBlock` — and nothing else. There is no way to put an image in
front of the model, from either direction:

- a **user** cannot attach a screenshot to a message;
- the **agent** cannot open one it finds on disk. `read` sniffs for a NUL byte in the
  first 8 KB and refuses binaries outright, suggesting `bash` instead.

Both are ordinary things to want. A failing UI test's screenshot, a diagram in a spec, a
scanned invoice in a fixture directory — all currently invisible.

## The options

### A. User attachments only

Add an `ImageBlock` to the unified types, implement it in all three adapters, and let a
message carry attachments. The agent still cannot open an image it discovers.

### B. A separate `view_image` tool

As A, plus a new tool that returns an image. `read` keeps refusing binaries. Small blast
radius; the model has to learn a second file-reading tool.

### C. `read` handles it

As A, plus `read` returns an image when the path is one. One tool, one mental model —
the way Claude Code does it. Costs the most: `ToolResultBlock.content` has to widen from
`str` to `str | list[ContentBlock]`, and that type is on the wire in all three adapters
and in the session's folding logic.

## The decision

**C.** One vocabulary for the model is worth the wider change.

`read` is already the answer to "what is in this file", and the agent reaches for it
without being told. A second tool that does the same job for a subset of files is a
thing to remember, and a thing to forget.

### What it touches

| | |
| --- | --- |
| `core/providers/types.py` | new `ImageBlock`; `ToolResultBlock.content` widens |
| `anthropic_claude.py` | `{"type": "image", "source": {"type": "base64", …}}` |
| `openai_gpt.py` | `{"type": "image_url", "image_url": {"url": "data:…"}}` |
| `google_gemini.py` | `Part.from_bytes` |
| `core/session/session.py` | a `user` record with attachments; `messages()` folds it |
| `core/harness/tools/files.py` | `read` returns an image for image types |
| `core/daemon/protocol.py` | `push` carries attachment paths |

### Two sub-decisions worth stating now

**The transcript stores paths, not bytes.** A `user` record carries the path and media
type; the bytes are read when the message is folded for a request. Base64 in the JSONL
would make a single screenshot larger than the rest of the session, and the file is
meant to stay readable with `jq`.

The cost is honest and should be written on the page: a session whose attachment has
since been deleted or edited **cannot be replayed exactly**. Append-only protects the
record of what was *said*, not the files it pointed at — and that is already true of
every `read` result in the transcript.

**Over-size images are refused, not downscaled.** Anthropic's limits are roughly 5 MB
and 1568px on the long edge; a full-size image also costs upwards of 1,600 tokens.
Resizing needs Pillow, which is a dependency for a problem the user can solve with any
tool on their machine. So: a clear error naming the limit and the actual size.

Pillow as an optional extra, with automatic downscaling when installed, is a reasonable
follow-up. It is not part of this decision.

## Consequences

- **Elision does not apply to images.** [Rule 1](../guide/tools.md) is about text, and
  an image is not elidable. An image result therefore has no `tool_out` spill and no
  hint; the cap has to be enforced *before* the read, by size, not after it.
- **The turn cost is lumpy.** One screenshot is roughly a 1,600-token tool result with
  no way to trim it. The `read` docstring should say so, so the model does not open six.
- **Gemini's tool-result path is narrower** than the other two. If image-in-tool-result
  turns out not to be expressible there, that adapter falls back to attaching the image
  as a following user message — a provider-boundary workaround, which is where
  provider differences belong.

## What would change this

If the widened `ToolResultBlock` turns out to complicate the session folding enough to
risk the "every `tool_use` has a `tool_result`" invariant, fall back to **B**. That
invariant is worth more than tool-count tidiness: breaking it is a 400 on the next
request and it is invisible in the JSONL.

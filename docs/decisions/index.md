# Decisions

One page per question that could reasonably have gone another way, written down with
the reasoning and the numbers **at the time it was decided**.

A decision record is not documentation of how the system works — that is
[Architecture](../architecture/index.md). It is documentation of *why it is not
something else*, so that the next person to have the same idea can read the arguments
instead of rediscovering them.

| | Decision | Status |
| --- | --- | --- |
| [0001](0001-tool-search.md) | Load tool definitions on demand | **Rejected**, with a narrower alternative |
| [0002](0002-file-and-image-input.md) | Files and images as model input | **Accepted**, not yet built |
| [0003](0003-what-the-tui-owns.md) | What the TUI is allowed to own | **Accepted** |

## Writing one

A record has five parts and no more:

1. **Context** — what prompted the question, with a measurement where one exists.
2. **The options**, stated fairly. An option written badly on purpose teaches nobody.
3. **The decision.**
4. **Consequences**, including the ones you would rather not have.
5. **What would change this**, so the record can be reopened honestly rather than
   argued around.

A record is never edited to agree with a later decision. It is superseded by a new one
that says so.

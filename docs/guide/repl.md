# The REPL and `tool_out`

`repl` runs Python in a persistent namespace — a plain `python -u` subprocess speaking
JSONL, with no notebook, no kernel protocol and no dependency.

```python
repl(code="import json; data = json.loads(open('report.json').read()); len(data['rows'])")
repl(code="[r for r in data['rows'] if r['status'] == 'failed'][:5]")
```

The second call sees `data`. That is the point: the alternative is re-reading and
re-parsing a large file on every turn.

## Why a subprocess

| | |
| --- | --- |
| Persistent namespace | variables survive between calls |
| Crash isolation | a segfault kills the worker, not the agent |
| SIGINT | a runaway cell can actually be interrupted |
| Zero dependencies | it is `python -u` and a pipe |

## `tool_out` — where elided output goes

This is the mechanism that makes [rule 1](tools.md#output-is-elided-never-summarised)
honest.

When a tool result exceeds its cap, the head and tail are kept, the middle is dropped,
and **the full value is injected into the REPL's namespace** under an `output_id`
before the model ever sees the elision:

```
grep found 1,204 matches
…
… [41,203 chars elided — the whole value is in tool_out["grep_a1b2"] — slice it with `repl`] …
```

```python
repl(code="[line for line in tool_out['grep_a1b2'].splitlines() if 'retry' in line]")
```

Nothing was lost. It moved somewhere cheaper, and the model was told where.

!!! note "The hint is only made when it is true"

    One place decides whether an elision may name `tool_out[...]`: whether a REPL is
    actually attached and holding the value. Without one, the hint instead says *call
    again with a narrower offset/limit* — an action that needs nothing that might not
    exist.

    An earlier version named a dict living in the harness process, which the model's
    REPL could not see. A model that followed the instruction got a `KeyError`. The fix
    was not a better variable name; it was that the promise and the fact are decided in
    the same place.

## The store is bounded

Spilled results are kept newest-first with a ceiling — 32 entries, 8 MB of text. A
long-running daemon that kept every large result it had ever produced would grow until
it was killed, and an `output_id` from forty tool calls ago is one nobody is going to
slice.

A single result larger than the whole budget evicts everything else and **stays** —
it is the one the model was just told about.

## Sub-agents do not get it

`repl` is not in the sub-agent tool set. A one-shot worker holding a persistent
namespace is doing the parent's job without the parent's oversight.

## Interrupting

++esc++ reaches a running cell as SIGINT. A cell that ignores it is escalated. The
awkward cases — what survives an interrupt, whether the reply you get back belongs to
the call you made — are exactly what the REPL's tests cover, because they are exactly
what a mock would have hidden.

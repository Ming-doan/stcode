# The supervisor

`core/agent/supervisor.py`. A second pair of eyes on the trajectory, at almost no cost.

```py
Supervisor.smell(records)            # -> str | None, zero tokens
await supervisor.check(records)      # -> a nudge, or None
supervisor.due(iteration)            # -> every `every` iterations within a turn
```

The session file is already the trajectory, so this is just a reader. NVIDIA's AVO result
on ARC-AGI-3 credited system design over model choice, and named a supervisor watching
for stagnation as part of it; this is the cheap version of that idea.

## Counting first, model second

Four plain-Python heuristics run over the last `window` (80) records. They cost nothing.
Only a hit spends one `difficulty="low"` call — and that model is explicitly allowed to
answer `NONE`.

| Heuristic | Fires when |
| --- | --- |
| Repetition | the same tool with the same arguments **3×** in the window |
| Failure rate | more than **half** the calls in the window returned `is_error` |
| Looking, not doing | **8** calls with no file written |
| Thrashing one file | the same file written or edited **4×** |

The thresholds are deliberately forgiving. At ~4 records per tool iteration (assistant,
tool_call, tool_result, usage), a window of 80 records observes ~20 iterations — broad
enough to observe real tool trends without drowning the slice in non-call events. Research
legitimately writes nothing for a long stretch, and a supervisor that fires on reading is
one you switch off — at which point it catches nothing at all.

## Three rules

* **Nudges go into `messages`, never the system prompt.** A `supervisor` record becomes a
  prefixed `user` message. Editing the cached prefix would cost a full cache miss for
  every piece of advice, which would make the cheap feature expensive.
* **The supervisor has no tools.** One that could write is a second agent, and a second
  agent needs its own supervisor.
* **It checks inside a turn**, every `every` (8) tool-call iterations, because that is
  where a loop happens. A task that loops does so within one turn, not across several.

It also refuses to repeat itself: the previous nudge is remembered, and an agent that
ignored the advice once will ignore the repeat. Saying it twice is itself a loop.

## What the model is asked

> You are watching another agent work, and you can see the last few minutes of what it
> did. Your only question: is it making progress, or is it stuck in a loop?
>
> Reply with exactly the word NONE if it is working normally. Trying something twice,
> reading before editing, and a failed command followed by a fixed one are all normal.
>
> Otherwise reply with one or two sentences, addressed to the agent, that name what it
> keeps doing and what to do instead. Be concrete — name the file, the command, or the
> assumption you think is wrong.

Addressed to the agent, because that is who will read it. A nudge phrased as a report to
a human ("the agent appears to be repeating…") arrives in the conversation as noise.

## What the agent sees

```jsonl
{"type":"supervisor","content":"You have grepped 'middleware' 4×. Read src/api/mw.py."}
```

and the client sees a `SupervisorNudge` event, so the agent changing direction is not
invisible to the person watching.

## Configuration

```toml
[supervisor]
enabled    = true
every      = 8          # tool-call iterations between checks, within a turn
window     = 80         # records the heuristics look at
difficulty = "low"      # the tier a hit spends
```

Sub-agents get no supervisor: a nudge would arrive about when a one-shot worker is
finishing anyway.

## Verifying it

`smoke_supervisor.py` drives a deliberately looping task against a real model and checks
that it is caught and redirected.

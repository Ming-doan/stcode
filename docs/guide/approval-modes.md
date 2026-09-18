# Approval modes

Four modes, one axis: **how much the agent may do without asking you.**

| Mode | Reads | Edits | Commands |
| --- | --- | --- | --- |
| `plan` | yes | **forbidden** | **forbidden** |
| `suggest` | yes | asks every time | asks every time |
| `auto-edit` | yes | yes | asks every time |
| `full-auto` | yes | yes | yes |

Change it with ++shift+tab++, `/mode <name>`, or `--mode` for one run. It takes effect
on the **live** session — the harness holds the mode and the tool list is rebuilt for
each request, so the next model call sees the new one.

A tool already in flight is not affected. That call was gated under the mode in force
when it started, and changing the rules underneath a running command is worse than
waiting for it.

## `plan` is not just "suggest with more asking"

In `plan` the write tools are **not advertised at all**. Being offered a capability and
then refused it wastes a turn and reads, to the model, like a bug to work around. The
prompt changes too: `plan` mode gets a prompt about researching and proposing, not
about making changes.

The prompt and the permission gate derive the mode from the same function, so they
cannot disagree.

## `full-auto` and the container rule

!!! danger "`full-auto` without an approver runs only inside a container"

    The daemon **refuses to start**. There is no override flag. This is code
    (`core/daemon/autonomy.py`), not a warning in a document.

The reasoning is not about trust, it is about blast radius. `full-auto` means the agent
runs commands with the daemon's privileges and nobody is asked. That is a reasonable
thing to want — it is how team mode works at all — and an unreasonable thing to allow
on a laptop with an SSH key in it.

A container is a real boundary, so the rule is: prove the boundary exists.

```bash
docker run -e STCODE_SANDBOX=1 … stcode --headless --mode full-auto
```

`STCODE_SANDBOX=1` is what a container sets. The guard also runs **per session**, not
just per daemon: a `full-auto` session on a `suggest` daemon is the same hole, so
`/mode full-auto` on a host daemon is refused too, and the client is told the mode did
not change rather than left showing one that was rejected.

## Which mode to use

| Situation | Mode |
| --- | --- |
| A change you have not scoped yet | `plan` |
| Normal work on code you care about | `suggest` |
| A refactor across many files, tests you trust | `auto-edit` |
| A container doing a job you already specified | `full-auto` |

## When nobody is attached

A headless daemon in `suggest` mode with no client attached cannot ask anyone. Rather
than claiming the user refused — which a model would believe and act on — the tool is
told:

> No client is attached to this session, so there is nobody to approve `bash`. Continue
> with what you can decide yourself and state the assumption you made.

That distinction matters: "the user said no" and "there was no user" should not produce
the same behaviour.

# 0007 — Separate CLI presentation, workflows and I/O

**Status: accepted.** Decided 2026-10-08.

## Context

The CLI has 4,601 lines in 15 flat Python files. `app.py` alone has 1,409 lines,
combining layout, streaming, socket lifecycle, commands, approvals and subprocesses.
Moving files alone would leave that ownership unchanged.

## The options

1. Keep the flat package and split the application into mixins. This reduces file
   size but leaves shared mutable state and implicit dependencies.
2. Separate UI, workflow logic and I/O services, grouping UI by screen and component.
   This introduces explicit ownership and allows workflow tests without a terminal.
3. Group everything by feature, including each feature's I/O and UI. This makes feature
   navigation easy but scatters socket and application lifecycle ownership.

## The decision

Choose option 2. `ui/` contains screens and components; `logic/` contains ordinary
Python workflows and state; `services/` contains socket, subprocess and filesystem
operations. `bootstrap.py` constructs dependencies. The daemon remains authoritative.

Use Textual messages for widget interactions and app-owned workers for the connection.
Use screen stylesheets and scoped widget defaults. These follow Textual's
[messages](https://textual.textualize.io/guide/events/),
[workers](https://textual.textualize.io/guide/workers/) and
[widgets](https://textual.textualize.io/guide/widgets/) APIs.
The package layout is our choice, not a Textual requirement.

## Consequences

There are more files, with one explicit direction of dependency. UI imports and test
paths change, while the `stcode.cli.main:run_cli` entry point remains stable. Workflows
cannot query widgets, and services cannot display errors. Widget handles stay in the
presenter instead of leaking into session state. Styles must ship in the wheel.

## What would change this

If multiple independent UI features acquire substantial workflows, their modules can
become packages within these layers. A second client may justify reusable client
logic, but must not introduce imports between `cli/` and `core/`.

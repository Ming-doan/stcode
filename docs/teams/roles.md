# Roles

A role is **one markdown file**. It is data, not code, and it is not shipped in the
package.

```
~/.stcode/agents/backend-dev.md
<project>/.stcode/agents/backend-dev.md
$STCODE_AGENTS_DIR/backend-dev.md
```

Searched nearest-first, with **no bundled fallback**. That is deliberate: a silent
fallback would mean a container whose volume failed to mount still starts — as somebody
else's backend dev. An unknown role name raises instead.

Copies to start from live in `examples/agents/`: `ba.md`, `backend-dev.md`,
`frontend-dev.md`, `devops.md`.

## What goes in one

```markdown
# Role: backend developer

## You own
`src/api/` and `src/services/` — the HTTP surface and the business logic behind it.

## You do not own
The database schema (that is `data-eng`), deployment, or anything under `infra/`.
Message the owner instead of editing across the line.

## How you report
When a change is ready, send the branch name and the paths you touched.
Never paste file contents into a message.
```

Three sections, and each one exists to prevent a specific failure:

| Section | The failure it prevents |
| --- | --- |
| **You own** | two agents editing the same file |
| **You do not own** | an agent helpfully fixing something outside its boundary |
| **How you report** | messages that carry content instead of paths |

## One agent = one role = one checkout = one merge boundary

If two roles need to write the same file, **the roles are split wrong**. That is a
design error, not a signal to add locking.

The boundary is drawn at the *service*, not the file, for exactly this reason: a split
that produces overlapping ownership produces merge conflicts that no amount of
coordination machinery fixes.

## The role is in the cached prefix

A role is static for the life of a container, so its text sits high in the system
prompt, above the general guidance and inside the cached prefix. It outranks the
general advice about what to work on, and it costs nothing after the first turn.

## Solo mode and roles

Naming a role in a solo session gives the agent that prompt. It does **not** turn team
mode on — that takes `[team] role`, which is a container declaring that a shared volume
is mounted. A solo session naming a role should not go looking for an inbox.

```bash
uv run stcode --role backend-dev   # sets [team] role, so this DOES join a team
```

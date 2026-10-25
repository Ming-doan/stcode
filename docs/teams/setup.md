# Set up a team

Give agents distinct roles when a project has work you want to coordinate across
separate workspaces. The current release supports stcode teammates that share a
mounted directory. Each agent needs its own model configuration and credentials.

## Prepare two profiles

From a stcode source checkout:

```bash
docker build -t stcode .
mkdir -p agents/backend agents/frontend team/knowledge team/artifacts
```

Create `agents/backend/config.toml`:

```toml
[model.providers.openai]
provider = "openai"
api_key = "${OPENAI_API_KEY}"

[agent]
approval_mode = "suggest"
prompt = "You work on the API. Coordinate interface changes with frontend-dev."

[team]
enabled = true
name = "my-project"
role = "backend-dev"
description = "Builds and tests the API."
shared_dir = "/team"
```

Copy this to `agents/frontend/config.toml` and change the role to `frontend-dev`,
the description to `Builds and tests the frontend.`, and the prompt to describe
that work. Each role name should be unique within the shared directory.

See [Roles](roles.md) for more examples. Set `model` in the provider entry if you
want a particular model for either agent.

## Start the agents

Export `OPENAI_API_KEY` in your shell. Prepare separate project checkouts for the
two roles, then replace the workspace paths below:

```bash
docker run -d --name stcode-backend   -e OPENAI_API_KEY   -p 127.0.0.1:7717:7717   -v "$PWD/agents/backend:/config"   -v "$PWD/team:/team"   -v /absolute/path/to/backend-checkout:/workspace   -v stcode-backend-sessions:/root/.stcode/sessions   stcode

docker run -d --name stcode-frontend   -e OPENAI_API_KEY   -p 127.0.0.1:7718:7717   -v "$PWD/agents/frontend:/config"   -v "$PWD/team:/team"   -v /absolute/path/to/frontend-checkout:/workspace   -v stcode-frontend-sessions:/root/.stcode/sessions   stcode
```

Both agents can share notes through `/team`. Their code stays in their individual
checkouts; use your usual Git branches and review workflow to combine changes.
Repository cloning and Git credentials are configured separately from team settings.

## Connect and give them work

Open a terminal for each role:

```bash
stcode --daemonless --transport tcp --host 127.0.0.1 --port 7717
stcode --daemonless --transport tcp --host 127.0.0.1 --port 7718
```

Confirm the connection in each UI. Opening the sessions makes both roles available
to their teammates. Start with a specific request such as:

```text
Define the search API response, save the contract in /team/knowledge/search.md,
and tell frontend-dev where to find it.
```

These profiles use `suggest`, so keep a client attached to each role to answer
approval requests. For unattended work inside the containers, set
`approval_mode = "full-auto"` in each profile before startup.

Stop them with `docker stop stcode-backend stcode-frontend` when finished. Their
workspace mounts and session volumes retain saved work.

Continue with [Team workflow](workflow.md) for handoffs and shared notes.

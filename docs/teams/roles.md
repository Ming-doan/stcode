# Roles

A role describes what an agent should work on and how teammates can find it.
Use a separate configuration file for each role.

## Write a profile

```toml
[model.providers.openai]
provider = "openai"
api_key = "${OPENAI_API_KEY}"

[team]
role = "backend-dev"
description = "Builds the API and maintains its interface documentation."

[agent]
approval_mode = "suggest"
prompt = """
Work on the API service. Keep the interface documentation up to date.
Coordinate response-format changes with frontend-dev. When work is ready,
report the branch, the changes, and the checks you ran.
"""
```

Choose a clear description: teammates use it to find the right agent for a task.
Useful profiles might cover the API, frontend, requirements, or release work;
choose the roles that fit your project.

For a longer prompt, put it in a separate file and use
`prompt_file = "backend-dev.md"` instead of `prompt`. Relative prompt paths are
resolved from the configuration file's directory. If both are set, nonempty
inline `prompt` takes precedence.

## Try a role locally

```bash
stcode --config agents/backend/config.toml
```

A role profile works without a team. To join teammates, add `enabled = true` and
`shared_dir` under `[team]`, as shown in [Setup](setup.md). Starting a profile
with a role name alone does not join a team.

If a local service is already running, finish its active work and use `--restart`
to load the new profile.

## Customize capabilities

Each profile can choose its own provider, model, approval mode, and
[available tools](../guide/tools.md). Skills and MCP servers can supply additional
project-specific workflows and integrations.

The repository's
[example profiles](https://github.com/Ming-doan/stcode/tree/main/examples/agents)
provide starting points. Adapt their prompts and workspace paths to your setup,
and explicitly set the shared directory for a team deployment.

# One image, one config file per agent. An agent profile is a `config.toml` carrying the
# prompt *and* the settings it runs under, and it is *mounted*, not built in: an agent is
# a deployment fact, and an image that carried four would make adding a fifth a release.
#
# No docker-compose.yml, no k8s manifests. This repo ships an image and the environment
# contract below; how you bring up N containers is yours, and orchestration opinions do
# not belong in a Python package.
#
#   docker build -t stcode .
#   docker run -d --name backend-dev \
#     -v team:/team -v /var/lib/stcode/backend:/workspace \
#     -v ./examples/agents/backend-dev.toml:/config/config.toml:ro \
#     -e STCODE_SANDBOX=1 -e STCODE_TEAM=1 -e ANTHROPIC_API_KEY \
#     -p 7717:7717 stcode
#
#   stcode --daemonless          # then /connect to localhost:7717
#
# STCODE_TEAM=1 is what turns team mode on. A profile naming a role is not enough on its
# own — a role says which agent this is, team mode says a shared volume is mounted.

FROM python:3.12-slim

# git is not optional here: every role clones from /team/repo.git before writing code.
# ripgrep arrives as a wheel with the package, so it is not installed twice.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the whole environment.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY stcode ./stcode
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    STCODE_CONFIG=/config/config.toml \
    STCODE_AGENTS_DIR=/agents \
    STCODE_SANDBOX=1
# STCODE_TEAM is deliberately not set: team mode is off until the deployment says so.
# STCODE_SANDBOX is what makes `full-auto` legal (rule 5). It is set here because this
# image *is* the container the rule is about — the daemon refuses to start in full-auto
# anywhere else, and there is no override flag.

# Mounts the contract expects:
#   /team       shared volume — inbox/, knowledge/, artifacts/, repo.git
#   /workspace  where this agent clones; one checkout per container
#   /config     this agent's profile, as config.toml, read-only
#   /agents     optional: a directory of <name>.toml profiles, read-only, for a host
#               selecting one with --role. A missing one refuses to start, which is what
#               you want when a mount silently did not happen.
VOLUME ["/team", "/workspace"]
WORKDIR /workspace

EXPOSE 7717

# Headless: the daemon alone, which is the whole point of daemon-first. It prints what
# it became — workspace, mode, model, config, what it created — and logs every client
# that connects, because `docker logs` is the only screen it has. Attach a TUI with
# `stcode --daemonless` and /connect, from anywhere that can reach the port; a
# containerised daemon takes one client at a time.
ENTRYPOINT ["stcode"]
CMD ["--headless", "--transport", "tcp", "--host", "0.0.0.0", "--port", "7717"]

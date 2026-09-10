# One image, every role. The role is chosen at run time by `[team] role`, because a
# role is a markdown file in the image rather than anything baked into the build.
#
# No docker-compose.yml, no k8s manifests. This repo ships an image and the environment
# contract below; how you bring up N containers is yours, and orchestration opinions do
# not belong in a Python package.
#
#   docker build -t stcode .
#   docker run -d --name backend-dev \
#     -v team:/team -v /var/lib/stcode/backend:/workspace \
#     -e STCODE_SANDBOX=1 -e OPENAI_API_KEY -e STCODE_ROLE=backend-dev \
#     -p 7717:7717 stcode
#
#   stcode --daemonless          # then /connect to localhost:7717

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
    STCODE_SANDBOX=1
# STCODE_SANDBOX is what makes `full-auto` legal (rule 5). It is set here because this
# image *is* the container the rule is about — the daemon refuses to start in full-auto
# anywhere else, and there is no override flag.

# Mounts the contract expects:
#   /team       shared volume — inbox/, knowledge/, artifacts/, repo.git
#   /workspace  where this agent clones; one checkout per container
#   /config     config.toml, read-only
VOLUME ["/team", "/workspace"]
WORKDIR /workspace

EXPOSE 7717

# Headless: the daemon alone, which is the whole point of daemon-first. Attach a TUI
# with `stcode --daemonless` and /connect, from anywhere that can reach the port.
ENTRYPOINT ["stcode"]
CMD ["--headless", "--transport", "tcp", "--host", "0.0.0.0", "--port", "7717"]

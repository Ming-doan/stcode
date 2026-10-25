# Installation

You need Python 3.10 or newer, a terminal, and credentials for a supported model
provider. Use Linux or macOS for local work; the container examples require Docker.

## Install with uv

```bash
uv tool install stcode
stcode --version
```

If you do not have uv, follow its [installation guide](https://docs.astral.sh/uv/getting-started/installation/).
To try stcode without a permanent tool installation:

```bash
uvx stcode
```

## Install with pip

In an activated Python environment:

```bash
pip install stcode
stcode --version
```

The package includes the search dependency; you do not need to install ripgrep
separately.

## Run from source

```bash
git clone https://github.com/Ming-doan/stcode
cd stcode
uv sync
uv run stcode
```

When running from this checkout, prefix the `stcode` commands in these docs with
`uv run`.

## Update

```bash
uv tool upgrade stcode
# Or, in the environment where you installed with pip:
pip install --upgrade stcode
```

Continue with the [quickstart](quickstart.md) to choose a provider and open a project.

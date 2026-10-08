"""
Config — the one place stcode's settings are located, read, resolved and written.

Every other module gets its values from a `Config` handed to it; none reads TOML or the
environment for a setting itself.

* **Read** — `load_config` parses the TOML into a validated `Config`. Environment
  switches (`STCODE_TEAM`, `STCODE_SUPERVISOR`, `STCODE_ROLE`) apply to that in-memory
  copy only.
* **Secrets** — a key is a literal or a `"${ENV_VAR}"` reference, resolved by `secret()`
  when it is used. The file only ever holds what was written to it.
* **Write** — `update_config` patches named keys in the TOML document. Comments and
  untouched keys survive, and a patch that would not load is refused before it lands.

A leaf: imports nothing from `stcode`, so anything in `core/` may import it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


# Enumerations shared by every layer
ApprovalMode = Literal["plan", "suggest", "auto-edit", "full-auto"]
Difficulty = Literal["low", "medium", "high"]
ProviderName = Literal["openai", "anthropic", "google"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
TraceProvider = Literal["langfuse", "phoenix"]

APPROVAL_MODES: tuple[ApprovalMode, ...] = ("plan", "suggest", "auto-edit", "full-auto")
DIFFICULTIES: tuple[Difficulty, ...] = ("low", "medium", "high")
PROVIDER_NAMES: tuple[ProviderName, ...] = ("openai", "anthropic", "google")
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

PROVIDER_KEY_ENV: dict[ProviderName, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
}
"""Where a library's key conventionally lives — the fallback when `api_key` is unset."""

ENV_TRUE = ("1", "true", "True", "yes")


# Locations and secrets
def home_dir() -> Path:
    """`~/.stcode`, or `%APPDATA%\\stcode` on Windows. Override with `STCODE_HOME`."""
    env_path = os.environ.get("STCODE_HOME")
    if env_path:
        return Path(env_path).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "stcode"
    return Path.home() / ".stcode"


def config_path() -> Path:
    """The config file: `$STCODE_CONFIG` when set, else `<home>/config.toml`."""
    env_path = os.environ.get("STCODE_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return home_dir() / "config.toml"


_ENV_REF = re.compile(r"\$\{(\w+)\}")


def secret(value: SecretStr | str | None) -> str | None:
    """A configured value, with a whole-value `"${ENV_VAR}"` reference resolved.

    None for an unset value or an unset variable, so `secret(x) or fallback` reads well.
    """
    if value is None:
        return None
    text = (value.get_secret_value() if isinstance(value, SecretStr) else value).strip()
    match = _ENV_REF.fullmatch(text)
    if match:
        return os.environ.get(match.group(1)) or None
    return text or None


def is_reference(value: SecretStr | str | None) -> bool:
    """Whether a value names an environment variable rather than holding the secret."""
    if value is None:
        return False
    text = value.get_secret_value() if isinstance(value, SecretStr) else value
    return bool(_ENV_REF.fullmatch(text.strip()))


# Sections
class DaemonConfig(BaseModel):
    """Where the daemon listens.
    Args:
        transport: `unix` — socket via `socket` path, `tcp` — socket via `host` and `port`.
        socket: Path to the Unix socket file. Ignored if `transport` is `tcp`.
        host: Hostname to bind to. Ignored if `transport` is `unix`.
        port: Port to bind to. Ignored if `transport` is `unix`.
        max_clients: How many clients may be connected at once. 0 = unlimited. Unset =
            1 in a container, unlimited on the host.
    """
    transport: Literal["unix", "tcp"] = "unix"
    socket: Path = Field(default_factory=lambda: home_dir() / "daemon.sock")
    host: str = "127.0.0.1"
    port: int = 7717
    max_clients: int | None = Field(default=None, ge=0)

    @property
    def address(self) -> str:
        if self.transport == "unix":
            return str(self.socket.expanduser())
        return f"{self.host}:{self.port}"


class AgentConfig(BaseModel):
    """Limits on one turn of the loop.
    Args:
        reasoning_effort: How much reasoning to ask for from the LLM.
        approval_mode: How much to gate tool calls on human approval.
        max_turns: How many tool calls round in one turn. `0` = unlimited.
        max_depth: How many sub-agent calls round in one turn. `1` = no recursion.
        max_concurrent: How many LLM calls may run at once. `0` = unlimited.
        enable_task: Enable sub-agent by include `task` tool.
        difficulty: The tier main agent runs at.
        tools: Allow-list of tools this deployment wants. Replaces the default.
        exclude_tools: Subtraction from whatever is left after `tools`.
        prompt: Inline prompt text for this agent profile.
        prompt_file: Path to a prompt file. Relative to the config file's directory.
    """
    reasoning_effort: ReasoningEffort = "medium"
    approval_mode: ApprovalMode = "suggest"
    max_turns: int = Field(default=40, ge=0)
    max_depth: int = Field(default=1, ge=1)
    max_concurrent: int = Field(default=4, ge=0)
    enable_task: bool = True
    difficulty: Difficulty = "high"
    tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)
    prompt: str = ""
    prompt_file: Path | None = None


class SessionConfig(BaseModel):
    """Where transcripts live.
    Args:
        dir: Path to the session directory. Defaults to `~/.stcode/sessions`.
        keep: How many sessions to keep before pruning. 0 = unlimited.
    """
    dir: Path = Field(default_factory=lambda: home_dir() / "sessions")
    keep: int = Field(default=100, ge=0)


class ProviderConfig(BaseModel):
    """One provider account. The table name is free (`[model.providers.work]`), so two
    keys for one library are two entries.
    Args:
        provider: Which library talks to it.
        model: The model used when no routing tier applies. Empty = the library's default.
        api_key: A literal key or `"${ENV_VAR}"`. Unset = the library's conventional variable.
        base_url: A proxy or compatible endpoint. Also accepts `"${ENV_VAR}"`.
        max_concurrent: Requests this endpoint serves at once. 0 = no cap; 1 for a local model.
    """
    provider: ProviderName = "openai"
    model: str = ""
    api_key: SecretStr | None = None
    base_url: str | None = None
    max_concurrent: int = Field(default=0, ge=0)

    def key(self) -> str | None:
        return secret(self.api_key) or os.environ.get(PROVIDER_KEY_ENV[self.provider]) or None

    def url(self) -> str | None:
        return secret(self.base_url)


class RouteConfig(BaseModel):
    """A difficulty tier's target.
    Args:
        provider: The name of an entry in `[model.providers]`.
        model: The model to call.
    """
    provider: str
    model: str


class RetryConfig(BaseModel):
    """How the daemon retries failed LLM calls. Not middle dropouts.
    Args:
        max_attempts: How many times to retry a failed call. 1 = no retries.
        base_delay: How long to wait before the first retry, in seconds.
        max_delay: The maximum delay between retries, in seconds.
        jitter: Whether to add random jitter to the delay.
    """
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay: float = Field(default=1.0, ge=0)
    max_delay: float = Field(default=20.0, ge=0)
    jitter: bool = True


class ModelConfig(BaseModel):
    """LLM provider configuration.
    Args:
        default: The provider entry used when no routing tier applies. Empty = the first.
        providers: Provider entries by name.
        routing: Per-difficulty overrides. Optional.
        retry: The retry configuration for failed LLM calls.
    """
    default: str = ""
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    routing: dict[Difficulty, RouteConfig] = Field(default_factory=dict)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @model_validator(mode="after")
    def check_names(self) -> ModelConfig:
        named = [self.default] if self.default else []
        named += [route.provider for route in self.routing.values()]
        unknown = sorted({name for name in named if name not in self.providers})
        if unknown:
            raise ValueError(
                f"[model] names provider(s) {', '.join(unknown)} with no "
                f"[model.providers.<name>] entry. Configured: {', '.join(self.providers) or 'none'}."
            )
        return self

    def default_name(self) -> str:
        return self.default or next(iter(self.providers), "")

    def route(self, difficulty: Difficulty) -> tuple[str, str]:
        """`(provider entry, model)` for a tier: its routing line, else the default entry.
        The model may be empty, meaning the library's default."""
        route = self.routing.get(difficulty)
        if route is not None:
            return route.provider, route.model
        name = self.default_name()
        if not name:
            raise ValueError("No provider configured. Add a [model.providers.<name>] table.")
        return name, self.providers[name].model


class TeamConfig(BaseModel):
    """Team mode config.
    Args:
        enabled: Whether team mode is on. Can also be set via `STCODE_TEAM=1`.
        name: The team this agent belongs to. Written on its member card.
        role: The agent's role name. Can also be set via `STCODE_ROLE`.
        description: One line on what this agent does — what `find_teammate` shows.
        shared_dir: Path to the shared directory for team mode.
        url: Reserved for a team service, used instead of `shared_dir`. Not built yet.
        wake_on_message: Whether the daemon starts a turn when a message arrives and the agent is idle.
        poll_interval: How often to poll for new messages in seconds.
    """
    enabled: bool = False
    name: str = ""
    role: str = ""
    description: str = ""
    shared_dir: Path = Field(default_factory=lambda: home_dir() / "team")
    url: str = ""
    wake_on_message: bool = True
    poll_interval: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def override_from_env(self) -> TeamConfig:
        if os.environ.get("STCODE_TEAM") in ENV_TRUE:
            self.enabled = True
        self.role = os.environ.get("STCODE_ROLE") or self.role
        return self


class SupervisorConfig(BaseModel):
    """The second pair of eyes on the trajectory.
    Args:
        enabled: Whether the supervisor is on. Can also be set via `STCODE_SUPERVISOR=1`.
        every: How many tool calls within a turn to count before checking for loops.
        window: How many tool calls to remember for loop detection.
        difficulty: The tier the supervisor runs at.
    """
    enabled: bool = False
    every: int = Field(default=8, ge=1)
    window: int = Field(default=80, ge=1)
    difficulty: Difficulty = "low"

    @model_validator(mode="after")
    def override_enabled_from_env(self) -> SupervisorConfig:
        if os.environ.get("STCODE_SUPERVISOR") in ENV_TRUE:
            self.enabled = True
        return self


class MCPConfig(BaseModel):
    """How MCP servers reach the agent.
    Args:
        enabled: Whether MCP is on. Default `True`.
        expose: `code` writes servers as importable files, `tools` advertises them per turn.
    """
    enabled: bool = True
    expose: Literal["code", "tools"] = "code"


class TraceConfig(BaseModel):
    """Direct HTTP observation provider. Unset values fall back to the provider's own
    conventional environment variables.
    Args:
        enabled: Whether trace is on. Default `False`.
        provider: Which trace provider to use. Default `langfuse`.
        url: The base URL of the trace provider.
        public_key: The public key for the trace provider. (Langfuse)
        secret_key: The secret key for the trace provider. (Langfuse)
        api_key: The API key for the trace provider. (Phoenix)
        project_name: The project name for the trace provider. (Phoenix)
        content: Whether to include message content in traces.
    """
    enabled: bool = False
    provider: TraceProvider = "langfuse"
    url: str = ""
    public_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    api_key: SecretStr | None = None
    project_name: str = ""
    content: bool = False

    def resolved(self) -> dict[str, str]:
        """Every value with its reference resolved and its conventional fallback applied."""
        env = os.environ.get
        if self.provider == "langfuse":
            return {
                "url": secret(self.url) or env("LANGFUSE_BASE_URL") or env("LANGFUSE_HOST")
                or "https://cloud.langfuse.com",
                "public_key": secret(self.public_key) or env("LANGFUSE_PUBLIC_KEY") or "",
                "secret_key": secret(self.secret_key) or env("LANGFUSE_SECRET_KEY") or "",
            }
        return {
            "url": secret(self.url) or env("PHOENIX_COLLECTOR_ENDPOINT") or "http://localhost:6006",
            "api_key": secret(self.api_key) or env("PHOENIX_API_KEY") or "",
            "project_name": self.project_name or env("PHOENIX_PROJECT_NAME") or "default",
        }


class ToolConfig(BaseModel):
    """Credentials for the built-in tools.
    Args:
        tavily_api_key: A literal key or `"${ENV_VAR}"`. Unset = `$TAVILY_API_KEY`.
    """
    tavily_api_key: SecretStr | None = None

    def tavily_key(self) -> str:
        return secret(self.tavily_api_key) or os.environ.get("TAVILY_API_KEY", "").strip()


class Config(BaseModel):
    """The whole config file, as read from TOML.

    `home`, `path` and `cwd` are where it came from, never written back: `home` holds
    the config, sessions and agent profiles, `path` is the file itself, and `cwd` is
    the workspace a session opens in when its client names none.

    An unknown top-level section is an error, so a file in an older layout
    (`[defaults]`, `[providers.*]`) says so instead of loading as an empty config.
    """

    model_config = ConfigDict(extra="forbid")

    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    team: TeamConfig = Field(default_factory=TeamConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)

    home: Path = Field(default_factory=home_dir, exclude=True)
    path: Path | None = Field(default=None, exclude=True)
    cwd: Path = Field(default_factory=Path.cwd, exclude=True)


# Reading and writing
DEFAULT_CONFIG = """\
# stcode config. A secret is a literal or a "${ENV_VAR}" reference.
# The daemon rewrites single keys in place, so comments here survive.

[model]
# default = "openai"            # the entry used when no [model.routing] tier applies

[model.providers.openai]
provider = "openai"             # or "anthropic", "google"
api_key = "${OPENAI_API_KEY}"
# model = "gpt-5"               # unset = the library's default
# base_url = "http://localhost:11434/v1"
# max_concurrent = 1            # for a local model

# [model.routing.low]           # optional, per difficulty tier
# provider = "openai"
# model = "gpt-5-mini"

[agent]
reasoning_effort = "medium"
approval_mode = "suggest"       # plan | suggest | auto-edit | full-auto
"""


def load_config(
    path: str | Path | None = None,
    cwd: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    create_if_missing: bool = False,
) -> Config:
    """Read and validate a config file.

    `overrides` are dotted keys (`{"daemon.transport": "tcp"}`) applied on top for this
    process only — command-line flags, never written back. `create_if_missing`
    scaffolds `DEFAULT_CONFIG` first.
    """
    path = Path(path).expanduser() if path else config_path()
    if create_if_missing and not path.exists():
        _write(path, DEFAULT_CONFIG)
    if not path.exists():
        raise FileNotFoundError(f"No stcode config found at {path}.")
    data = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    for dotted, value in (overrides or {}).items():
        _set(data, dotted, value, table=dict)
    config = Config.model_validate(data)
    config.home = path.parent
    config.path = path
    config.cwd = cwd or Path.cwd()
    return config


def update_config(path: Path, changes: dict[str, Any]) -> None:
    """Set dotted keys (`"agent.approval_mode"`) in the file; `None` removes one.

    Only the named keys change — comments and everything else survive. Refused with a
    `ValueError` before anything is written when the result would not load.
    """
    document = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    for dotted, value in changes.items():
        _set(document, dotted, value, table=lambda: tomlkit.table(is_super_table=True))
    Config.model_validate(document.unwrap())
    _write(path, tomlkit.dumps(document))


def _set(root: Any, dotted: str, value: Any, *, table: Any) -> None:
    """Set (or, for `None`, remove) one dotted key, creating tables on the way."""
    *parents, leaf = dotted.split(".")
    for part in parents:
        if part not in root:
            root[part] = table()
        root = root[part]
    if value is None:
        root.pop(leaf, None)
    else:
        root[leaf] = value


def _write(path: Path, text: str) -> None:
    """Atomically, mode 0600: the file may hold a literal key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def resolve_prompt(config: Config) -> str:
    """The `[agent]` prompt: inline `prompt`, else the contents of `prompt_file`."""
    prompt, prompt_file = config.agent.prompt.strip(), config.agent.prompt_file
    if prompt or prompt_file is None:
        return prompt
    target = prompt_file.expanduser()
    if not target.is_absolute():
        target = config.home / target
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        # Loud: a prompt file that did not mount is an agent that owns nothing, and it
        # looks exactly like one that did until it starts working.
        raise FileNotFoundError(
            f"[agent] prompt_file points at {target}, which cannot be read ({exc})."
        ) from exc

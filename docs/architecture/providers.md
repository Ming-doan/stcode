# Providers and the gateway

`core/providers/`. One adapter per SDK, and one gateway in front of them.

```py
async with LLMGateway(providers={...}, routing={...}, retry={...}) as gw:
    async for event in gw.stream(messages, system=..., tools=..., difficulty="high"):
        ...
```

The gateway knows nothing about sessions, tools, or the agent loop. It resolves a
difficulty tier to a provider + model + credentials, retries transient failures, and
streams back unified events. One instance per configuration, not a singleton; provider
clients are cached inside by `(provider, key, base_url)`, so a daemon holding eight
sessions holds one connection pool.

It deliberately does **not** run an agent loop. Tool execution, turn-by-turn tool sets
and human-in-the-loop approval all vary per call and live above this layer; baking a loop
in would mean either re-exposing every provider knob through the gateway or hard-coding
one harness's shape into a "send this, stream that back" API.

## Difficulty routing

```toml
[routing.low]     provider = "anthropic"   model = "claude-haiku-4-5"
[routing.medium]  provider = "anthropic"   model = "claude-sonnet-5"
[routing.high]    provider = "anthropic"   model = "claude-opus-5"
```

Three tiers, not model names at call sites. The caller says how hard the work is; the
config says what that costs. `difficulty="low"` is what the supervisor spends and what a
mechanical sub-agent should be given; the main loop defaults to `high`.

A tier may override `api_key_env` / `api_key` / `base_url` — for a higher-quota key
reserved for `high` — and falls back to the provider's main entry when it does not.
`resolve_secret` decides between an env var name and a literal, and **the env var wins
when set**, so a config file in a repository never overrides the shell.

`stream(provider=..., model=...)` bypasses tier routing entirely, using that provider's
main credentials. That is for `stcode config` and for tests, not for the loop.

## How many at once

```toml
[providers.openai]
base_url       = "http://10.0.0.3:11434/v1"
max_concurrent = 1
```

One cap per **endpoint**, applied around the whole completion rather than around the
request, because what an inference server is rationing is streams it is serving. `0`,
the default, is no cap.

The cap belongs here and not in `[agent]`, because the thing being protected is the
server and the thing overwhelming it is not one agent. A daemon shares one gateway
across every session, every sub-agent and the supervisor — so a model that answers a
question by dispatching five `task` calls opens five simultaneous completions, and it
does not matter that each sub-agent thought it was making one request.

**A hosted API absorbs that. A local one does not.** Ollama or llama.cpp serving a 27b
model on one GPU takes one request at a time, queues the rest, and drops whatever waited
past its budget:

```
[the sub-agent did not finish] APIError: [503] Request dropped after exceeding the
local rate-limit queue budget maxWaitMs (15000ms)
```

Retry cannot fix that — nothing about it was transient, and three more attempts arrive
into the same full queue. `max_concurrent = 1` makes the five calls run one after
another instead, which is slower than the parallelism the model asked for and faster
than five failures.

The semaphore is held for the life of the stream and released in the generator's
`finally`, so a caller that abandons a stream frees its slot. Every caller wraps
iteration in `aclosing` for exactly this reason. → [the agent loop](agent-loop.md)

## Changing the configuration under a running agent

`reconfigure()` replaces the providers, routing and retry policy **in place** and closes
the cached clients, because a client is built around the key and base URL being
replaced.

In place rather than by building a new gateway, because of who holds the reference: a
`/model` that pastes a new key has to reach the session that is already running, and
that session's `Agent` holds this object, not the daemon that built it. Swapping the
daemon's gateway would leave the running turn streaming against the old credentials
until it ended. → [the daemon](daemon.md)

## Retry

`max_attempts`, exponential backoff from `base_delay` to `max_delay`, optional jitter.

**Only before the first token.** Once anything has been yielded the caller holds partial
content, and retrying would duplicate it — so `started` gates the whole retry decision.

Retryable means transient: connection errors, timeouts, rate limits, 5xx. Anything else
(bad request, auth, not found, content policy) is a caller or config bug and surfaces
immediately; retrying it three times only delays the message you need to read.

## The unified wire format

`core/providers/types.py` is the vocabulary everything above this layer speaks:

| | |
| --- | --- |
| `Message` | `role` + `content`, where content is text or a list of blocks |
| `TextBlock` / `ToolUseBlock` / `ToolResultBlock` | what a message can carry |
| `TextDelta` / `ReasoningDelta` | streamed output |
| `ToolCallStart` / `ToolCallDelta` / `ToolCallEnd` | a tool call, accumulated |
| `MessageStop` | `stop_reason` + the four-field `Usage` |

**Normalise in the adapter, never above it.** OpenAI streams
`tool_calls[].function.arguments` as string fragments; Anthropic uses
`content_block_delta` with an `input_json_delta`. Both become `ToolCallEnd` with a parsed
dict before anyone else sees them. A caller that has to know which provider it is talking
to is a caller that will only work with one.

`Usage` carries four fields — `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens` — all the way to the client.
The cache counters are the evidence for the caching claim, and the one screen that could
show them cannot if any layer drops them to `{in, out}`.

## Prompt caching

`cache_control` lives in `anthropic_claude.py` and nowhere else.

What makes it work is not the adapter, though — it is that the system prompt and tool
definitions are **byte-stable across turns**:

* tool definitions are emitted sorted by name, so the same tool set serialises
  identically every time;
* the prompt's static sections come first and never vary within a session;
* supervisor nudges and team messages arrive as `user` **messages**, never as edits to
  the system prompt — appending to the conversation costs a cache write, editing the
  prefix costs a full miss on every following turn.

This is the single biggest cost lever in the system. Treat any change that makes an early
prompt section dynamic as a performance regression, because it is one.

## Adding a provider

1. Implement `BaseModelProvider` in `core/providers/<name>.py`: `stream()`,
   `list_models()`, `aclose()`.
2. Convert `Message` → the SDK's shape on the way in, and the SDK's stream → `StreamEvent`
   on the way out. Nothing else in the codebase may learn the SDK's names.
3. Register it in `registry.py` with its default model and its conventional key env var.
4. It is now selectable from `[providers]` and any `[routing]` tier.

An OpenAI-compatible endpoint needs none of that — point `base_url` at it.

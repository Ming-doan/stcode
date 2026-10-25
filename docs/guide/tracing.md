# Tracing

Review agent turns, model calls, tool activity, and token usage in Langfuse or
Phoenix. Tracing is optional and off by default. No extra Python package is needed.

## Connect Langfuse

Add this to `config.toml`:

```toml
[trace]
enabled = true
provider = "langfuse"
url = "https://cloud.langfuse.com"
public_key = "${LANGFUSE_PUBLIC_KEY}"
secret_key = "${LANGFUSE_SECRET_KEY}"
content = false
```

Export the keys from your Langfuse project before starting stcode. Use your own
base URL for a regional or self-hosted deployment.

## Connect Phoenix

Create a project in Phoenix, then use its name and base URL:

```toml
[trace]
enabled = true
provider = "phoenix"
url = "http://localhost:6006"
project_name = "stcode"
api_key = "${PHOENIX_API_KEY}"
content = false
```

A local Phoenix instance without authentication does not need `api_key`. Your
Phoenix server must support its project spans REST API.

## Environment configuration

Configured values take precedence. When a setting is omitted, these variables
provide defaults:

| Provider | Variables |
| --- | --- |
| Langfuse | `LANGFUSE_BASE_URL` or `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` |
| Phoenix | `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_API_KEY`, `PHOENIX_PROJECT_NAME` |

Export variables in the process that starts stcode. `.env` files are not loaded
automatically. After changing tracing settings, restart the service to apply them;
wait for current work to finish first.

## Choose what to record

With `content = false`, traces include activity metadata, model names, timing,
token usage, and error types. Prompt text, replies, and tool inputs and results
are omitted. Set `content = true` to include them when you want to inspect a
conversation in detail. This sends that content to your chosen tracing service.

## Review a run

Run a task, then open your tracing dashboard. Find the agent turn and inspect its
model calls and tools. In Langfuse's observations view, filter to root observations
to find individual turns, then open one to explore its activity.

If nothing appears, check the service URL, credentials, project, and network
access from the machine running stcode. Trace export problems are reported in
service logs and do not stop coding tasks.

For local usage totals without a tracing service, use `/token` in the terminal.

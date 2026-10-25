# Local and remote work

Use stcode on your machine, attach to a workspace elsewhere, or run without a
terminal UI.

| Command | Use |
| --- | --- |
| `stcode` | Open the terminal UI and start a local background service if needed |
| `stcode --headless` | Run the service without a UI |
| `stcode --daemonless` | Open a UI that connects to an existing service |

## Work locally

```bash
stcode ~/projects/api
```

The selected directory is the workspace for the conversation. You can leave the
UI and return through [sessions](sessions.md) while the service keeps running.

## Connect to a remote workspace

On the workspace host, start a service:

```bash
stcode --headless --transport tcp --host 127.0.0.1 --port 7717
```

For another machine, forward the port over SSH from your laptop:

```bash
ssh -N -L 7717:127.0.0.1:7717 user@workspace-host
```

Then open a separate terminal:

```bash
stcode --daemonless --transport tcp --host 127.0.0.1 --port 7717
```

Confirm the address in the connection screen. `/connect` lets you switch to a
different service. TCP connections should stay on a trusted network or behind an
SSH tunnel; the service does not provide authentication or TLS.

The workspace, files, commands, and saved sessions belong to the connected service.
`@` lists its files, and `!` runs commands there. Configure provider credentials
on the host before connecting; the remote settings UI does not accept API keys.

## Run in Docker

From a source checkout, build the image:

```bash
docker build -t stcode .
```

Prepare a configuration file using the [configuration guide](../getting-started/configuration.md),
then start a container. Replace the paths with your configuration directory and
project:

```bash
docker run -d --name stcode-workspace   -e OPENAI_API_KEY   -p 127.0.0.1:7717:7717   -v /absolute/path/to/config:/config   -v /absolute/path/to/project:/workspace   -v stcode-sessions:/root/.stcode/sessions   stcode
```

The configuration directory should contain `config.toml`. The image starts the
service over TCP; connect using the `--daemonless` command above. Mount only the
workspace and credentials you intend the agent to use.

For unattended execution, append the service options:

```text
--transport tcp --host 0.0.0.0 --port 7717 --mode full-auto
```

Starting a headless service does not submit a task. Connect a terminal to give it
work. For scripts that submit tasks directly, use the [Python SDK](../sdk/embedding.md).

## Stop or restart

For a foreground service, press **Ctrl+C**. Stop a container with
`docker stop stcode-workspace`. To reload a local service, use `stcode --restart`;
this interrupts its active work.

For several collaborating agents, see [Teams](../teams/setup.md).

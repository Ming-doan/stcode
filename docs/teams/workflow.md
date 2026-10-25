# Team workflow

Start by giving each agent a clear task and an expected result. Agents can find
teammates, send handoffs, and share notes while you follow their work in separate
terminal sessions.

## Plan a handoff

For an API and frontend change, a useful sequence is:

1. Ask the API agent to define the response and save a contract in the shared directory.
2. Ask it to notify the frontend agent when the contract is ready.
3. Let each agent implement and test its part in its own checkout.
4. Review the branches and integrate the changes using your usual Git workflow.

For example:

```text
Find the teammate responsible for the frontend. Let them know that the search
contract is ready at /team/knowledge/search.md and ask them to check the fields.
```

## Find teammates

The agent uses `find_teammate` to discover roles and their descriptions.
Open a session for each role before asking agents to find one another. If a role
is missing, check that its team configuration is enabled and uses the same
shared directory.

## Share useful context

Use the shared directory for contracts, design notes, or reports. Ask agents to
send a short message with paths to the relevant files. The `send_team_message`
tool sends the handoff to a named role.

A path in one private checkout may not exist for another role. Put shared material
in `/team`, or provide a branch and commit that the recipient can fetch.

## Continue when a message arrives

Idle teammates can start working when they receive a message. This is enabled by
default; turn it off if you want to start each turn yourself:

```toml
[team]
wake_on_message = false
```

A busy agent receives team messages when it begins its next turn. You can always
connect to its terminal to clarify a task or interrupt active work. The agent's
approval mode still applies to work triggered by a message.

## Choose the right scope

Teams are useful for work with distinct responsibilities and clear handoffs.
For a focused change, a single agent may be enough. Each active agent makes its
own model calls, so review usage with `/token` or [tracing](../guide/tracing.md).

## Protocol availability

Built-in team messaging currently connects stcode agents through a shared
directory. Broader agent-to-agent protocol support is planned; native A2A and ACP
adapters are not available in this release. [MCP servers](../guide/mcp.md) can
connect external tools, including an agent service if it exposes an MCP interface.

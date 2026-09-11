"""
Agent — the turn loop, and the sub-agent tool that spawns another one.

    agent = await Agent.create(config, cwd=workspace, role="backend-dev")

    async for ev in agent.run("Add rate limiting to the API"):
        match ev:
            case TextDelta():     out(ev.text)
            case ToolStarted():   spinner(ev.name)
            case ToolFinished():  done(ev.name)
            case TurnFinished():  idle(ev.usage)
            case AgentFailed():   error(ev.message)

`push()` + `events()` is the same machine with the door left open — see `agent.py`.
"""

from stcode.core.agent.agent import DEFAULT_MAX_TURNS, Agent
from stcode.core.agent.events import (
    AgentEvent,
    AgentFailed,
    ReasoningDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from stcode.core.agent.task import MAX_DEPTH, make_task_tool

__all__ = [
    "DEFAULT_MAX_TURNS",
    "MAX_DEPTH",
    "Agent",
    "AgentEvent",
    "AgentFailed",
    "ReasoningDelta",
    "TextDelta",
    "ToolFinished",
    "ToolStarted",
    "TurnFinished",
    "make_task_tool",
]

"""Scrollable transcript container."""

from textual.containers import VerticalScroll
from textual.widget import Widget
from .entries import AgentRow


class Transcript(VerticalScroll):
    """The conversation. Entries are widgets; a sub-agent's are wrapped in a gutter."""

    DEFAULT_CSS = """
    Transcript { height: 1fr; scrollbar-size-vertical: 1; }
    
    .entry {
        height: auto;
        margin-bottom: 1;
    }
    
    .entry.platform {
        margin-bottom: 1;
    }
    
    /* What you said, tinted. Without it a short prompt is three words of plain text in
       a column of plain text, and scrolling back to find where you asked something
       means reading rather than looking. */
    .entry.message.user {
        width: 1fr;
        padding: 1 1;
        background: $primary 12%;
    }
    
    /* A `!` command: a rule down the left and nothing else. It is neither the model's
       nor the platform's, and none of it is in the session. */
    .entry.shell {
        width: 1fr;
        padding: 0 1;
        height: 6;
        border-left: solid $primary;
        scrollbar-size-vertical: 1;
    }
    
    .entry.tool {
        background: $foreground 6%;
        color: $foreground;
        border-left: solid $primary;
        padding: 1 1;
    }

    .entry.thinking, .entry.progress {
        max-height: 6;
        scrollbar-size-vertical: 1;
    }
    
    /* Full width and tinted: "no API key" printed dim among tool output is a message
       people read twenty minutes after they needed it. */
    .entry.notice {
        width: 1fr;
        padding: 0 1;
        color: $foreground;
    }
    
    .entry.notice.error {
        background: $error 25%;
    }
    
    .entry.notice.warning {
        background: $warning 25%;
    }
    
    .entry.agent-row {
        height: auto;
    }
    
    /* The row already carries the gap. Without this the entry inside it adds a second
       one and a sub-agent's output reads as twice as far apart as the main agent's. */
    .agent-row .entry {
        margin-bottom: 0;
    }
    
    .agent-name {
        height: auto;
        padding-right: 1;
    }
    """

    def add(self, entry: Widget, *, agent: str = "") -> Widget:
        """Mount one entry and scroll to it. Returns the entry, not the wrapper, so a
        streaming caller keeps a handle on the thing it appends to."""
        self.mount(AgentRow(agent, entry) if agent else entry)
        self.scroll_end(animate=False)
        return entry

    def clear(self) -> None:
        self.remove_children()

    @property
    def scrolls(self) -> bool:
        """Whether there is more transcript than screen — what hides the banner."""
        return self.max_scroll_y > 0

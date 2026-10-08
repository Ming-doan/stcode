"""
TUI tests — the app, driven by keystrokes, against a real daemon.

Real socket, real protocol, real `Agent`; the only fake is the gateway
(`RecordingGateway`). A UI test that mocked the client would be a test of our beliefs
about the client, and the whole design claim here is that the screen is *just* a client.

What these protect is the keyboard. Every one of them is a thing a person does in the
first minute — press `?`, type a path with a slash in it, hit enter — and every one of
them is invisible in code review.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from typing import Any, Callable

from conftest import asynctest
from fakes import RecordingGateway, calls_tool, says
from textual.widgets import Button, Input, OptionList

from stcode.cli import labels
from stcode.cli.ui.app import StcodeApp
from stcode.cli.bootstrap import build_app
from stcode.cli.ui.components.cards import Card
from stcode.cli.services.client import Address
from stcode.cli.ui.screens.connect import ConnectScreen
from stcode.cli.services.launcher import Launcher
from stcode.cli.ui.screens.trust import TrustScreen
from stcode.cli.ui.screens.settings import SettingsScreen
from stcode.cli.services.preferences import UiPrefs, save_prefs
from stcode.cli.ui.components.prompt import Prompt
from stcode.cli.ui.components.transcript import (
    AgentRow,
    Message,
    Notice,
    PlatformNote,
    Progress,
    ShellOutput,
    ToolCall,
)
from stcode.core.configs import load_config
from stcode.core.daemon import Daemon
from stcode.core.providers.types import MessageStop, ToolCallEnd, Usage

CONFIG = """\
[model.providers.fake]
provider = "openai"
api_key = "test-key"
model = "fake-large"

[model.routing.medium]
provider = "fake"
model = "fake-small"

[model.routing.low]
provider = "fake"
model = "fake-small"

[agent]
approval_mode = "auto-edit"

[supervisor]
enabled = false

[mcp]
enabled = false
"""


class Harnessed:
    """A daemon on a unix socket in `tmp_path`, reading `config.toml` there, plus an app
    pointed at it. The app's launcher finds it listening and never starts another."""

    def __init__(
        self,
        tmp_path: Path,
        workspace: Path,
        gateway: RecordingGateway,
        *,
        trusted: bool = True,
        approval_mode: str = "auto-edit",
    ) -> None:
        self.config_path = tmp_path / "config.toml"
        self.config_path.write_text(CONFIG.replace('approval_mode = "auto-edit"', f'approval_mode = "{approval_mode}"'))
        self.prefs_path = tmp_path / "ui.toml"
        save_prefs(
            UiPrefs(theme="dark", trusted=[str(workspace)] if trusted else []), self.prefs_path
        )
        self.address = Address(socket=tmp_path / "d.sock")
        self.log_path = tmp_path / "daemon.log"
        overrides = {"session.dir": str(tmp_path / "sessions"), "daemon.socket": str(self.address.socket)}
        config = load_config(self.config_path, overrides=overrides)
        self.daemon = Daemon(config, gateway=gateway, overrides=overrides)  # type: ignore[arg-type]
        self.gateway = gateway
        self.workspace = workspace

    def app(self, **kwargs: Any) -> StcodeApp:
        return build_app(
            Launcher(self.address, log_path=self.log_path),
            cwd=self.workspace,
            prefs_path=self.prefs_path,
            terminal_mode="dark",
            **kwargs,
        )

    async def __aenter__(self) -> "Harnessed":
        await self.daemon.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.daemon.aclose()


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def connected(app: StcodeApp) -> None:
    assert await until(lambda: bool(app.state.session_id)), "the app never opened a session"


def card_of(app: StcodeApp) -> Card | None:
    return app.chat.cards.current


# ---- the symbol aliases ----------------------------------------------------------


@asynctest
async def test_a_question_mark_opens_help_and_inserts_nothing(
    tmp_path: Path, workspace: Path
) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("question_mark")
            await pilot.pause()

            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_HELP
            assert app.chat.prompt.text == "", "the trigger character is not inserted"
            # The paths are in the card, because they are not printed anywhere else.
            assert any(str(env.config_path) == detail for _label, detail in card.rows)


@asynctest
async def test_backspace_closes_the_help_card_and_the_next_one_is_a_character(
    tmp_path: Path, workspace: Path
) -> None:
    """The promise in the guide: nothing was inserted, so typing it again inserts it."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("backspace")
            await pilot.pause()
            assert card_of(app) is None
            assert app.chat.prompt.text == ""

            await pilot.press("question_mark", "question_mark")
            await pilot.pause()
            assert app.chat.prompt.text == "?"
            assert card_of(app) is None, "the card gets out of the way once there is text"


@asynctest
async def test_a_slash_opens_the_commands_card_and_typing_filters_it(
    tmp_path: Path, workspace: Path
) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("slash")
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_COMMANDS
            # Not `COMMANDS`: this app is not `--daemonless`, so `/connect` is not on
            # offer — it would move the terminal off the daemon it just started. The
            # workspace's own skills are on the end, because `/name` is how one is run.
            names = [row[0] for row in card.rows]
            assert "/connect" not in names
            assert names[: len(labels.COMMANDS) - 1] == [
                row[0] for row in labels.COMMANDS if row[0] != "/connect"
            ]
            assert "/db-migrations" in names and "/release-notes" in names

            await pilot.press("m", "o", "d")
            await pilot.pause()
            assert [row[0] for row in card.rows] == ["/model", "/mode"]

            # Backspacing the slash away closes it: the token is no longer a command.
            await pilot.press("backspace", "backspace", "backspace", "backspace")
            await pilot.pause()
            assert card_of(app) is None


@asynctest
async def test_a_path_with_a_slash_in_it_does_not_open_a_card(
    tmp_path: Path, workspace: Path
) -> None:
    """The bug people notice first: a `/` inside a path hijacking the input."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            for key in ("r", "e", "a", "d", "space", "s", "r", "c", "slash", "a"):
                await pilot.press(key)
            await pilot.pause()
            assert app.chat.prompt.text == "read src/a"
            assert card_of(app) is None


@asynctest
async def test_an_at_sign_offers_the_workspace_files(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("at")
            await pilot.pause()
            assert await until(lambda: bool(app.chat.files))
            await pilot.press("a", "p", "p")
            await pilot.pause()

            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_FILES
            assert any("app.py" in row[0] for row in card.rows)

            await pilot.press("enter")
            await pilot.pause()
            assert "app.py" in app.chat.prompt.text
            assert card_of(app) is None


@asynctest
async def test_the_first_at_sign_fills_itself_in_once_the_listing_arrives(
    tmp_path: Path, workspace: Path
) -> None:
    """The listing runs in a thread, so the first `@` opens a card before there is
    anything to put in it. Without refilling it, the first `@` of every session showed
    nothing and only worked after backspacing and typing it again."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            assert app.chat.files is None, "nothing is listed until the first `@`"

            await pilot.press("at")
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_FILES
            # While it is loading the card says so, and enter cannot insert that.
            if not card.selectable:
                assert card.rows == [(labels.FILES_LOADING, "")]

            assert await until(lambda: app.chat.files is not None)
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.selectable
            assert any("app.py" in row[0] for row in card.rows), "no keystroke needed"


# ---- skills ----------------------------------------------------------------------


@asynctest
async def test_choosing_a_skill_writes_its_name_into_the_input(
    tmp_path: Path, workspace: Path
) -> None:
    """The card names the skill; the sentence after it is yours. Firing on enter would
    leave nowhere to say what to do with it."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            assert await until(lambda: bool(app.state.info.get("skills")))

            app.chat._run_command("/skills")
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_SKILLS and card.selectable
            first = card.rows[0][0]

            await pilot.press("enter")
            await pilot.pause()
            assert app.chat.prompt.text.startswith(f"/{first}")
            assert card_of(app) is None


@asynctest
async def test_a_skill_command_reaches_the_agent_as_a_message(
    tmp_path: Path, workspace: Path
) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([says("on it")])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            assert await until(lambda: bool(app.state.info.get("skills")))
            name = str(app.state.info["skills"][0]["name"])

            app.chat._on_submit(Prompt.Submitted(f"/{name} do the thing"))
            await pilot.pause()
            sent = [
                entry.body
                for entry in app.chat.transcript.query(Message)
                if entry.has_class("user")
            ]
            assert sent == [labels.skill_request(name, "do the thing")]


@asynctest
async def test_an_unknown_slash_command_is_still_an_error(
    tmp_path: Path, workspace: Path
) -> None:
    """Skills joining the `/` list must not turn every typo into a message to the
    model."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.chat._on_submit(Prompt.Submitted("/nosuchthing"))
            await pilot.pause()
            assert any(
                "nosuchthing" in entry._text for entry in app.chat.transcript.query(Notice)
            )


# ---- a running tool's output -----------------------------------------------------


@asynctest
async def test_progress_is_one_block_not_a_rule_per_line(
    tmp_path: Path, workspace: Path
) -> None:
    """A REPL cell streams a line per frame.

    Rendered as platform notes, an MCP result printed from a cell became forty dashed
    rules with a word centred in each. One dim block, appended to, is what it is: the
    tool is still running.
    """
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            before = len(app.chat.transcript.query(PlatformNote))
            for line in ("- Title: Context7", "- Snippets: 1120", "----------"):
                app.chat.handle_frame({"type": "progress", "text": line})
            await pilot.pause()

            blocks = list(app.chat.transcript.query(Progress))
            assert len(blocks) == 1, "consecutive lines share one block"
            assert blocks[0].body.splitlines() == [
                "- Title: Context7",
                "- Snippets: 1120",
                "----------",
            ]
            assert len(app.chat.transcript.query(PlatformNote)) == before


# ---- sending, and what the transcript shows --------------------------------------


@asynctest
async def test_enter_sends_and_shift_enter_does_not(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([says("hello back")])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("h", "i")
            await pilot.press("shift+enter")
            await pilot.press("t", "h", "e", "r", "e")
            await pilot.pause()
            assert app.chat.prompt.text == "hi\nthere"
            assert not env.gateway.calls, "shift+enter must not send"

            await pilot.press("enter")
            await pilot.pause()
            assert app.chat.prompt.text == ""
            assert await until(lambda: bool(env.gateway.calls))
            assert [message.role for message in env.gateway.last.messages] == ["user"]
            assert any(isinstance(entry, Message) for entry in app.chat.transcript.children)


@asynctest
async def test_a_tool_call_renders_as_one_box_updated_in_place(
    tmp_path: Path, workspace: Path
) -> None:
    """Two boxes for one call would double the transcript and say nothing more."""
    gateway = RecordingGateway(
        [calls_tool("c1", "read", path="src/app.py"), says("that is the app")]
    )
    async with Harnessed(tmp_path, workspace, gateway) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            assert await until(lambda: len(gateway.calls) >= 2)
            await pilot.pause()

            boxes = [entry for entry in app.chat.transcript.children if isinstance(entry, ToolCall)]
            assert len(boxes) == 1
            assert app.chat.presenter.tools == {}, "the box was matched to its result and released"


@asynctest
async def test_a_sub_agent_is_indented_under_its_name(
    tmp_path: Path, workspace: Path, monkeypatch: Any
) -> None:
    """The events travel on the side channel and land in a gutter. Nothing about the
    parent's transcript changes because we watched.

    `STCODE_SANDBOX=1` because `full-auto` is legal only in a container (rule 5), and
    spawning a sub-agent is an `execute`: without it this test would be a test of the
    approval card instead.
    """
    monkeypatch.setenv("STCODE_SANDBOX", "1")
    gateway = RecordingGateway(
        [
            calls_tool("c1", "task", prompt="Find the handlers.", name="api-scout"),
            says("the scout looked"),  # the child's turn
            says("done"),
        ]
    )
    async with Harnessed(tmp_path, workspace, gateway, approval_mode="full-auto") as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            assert await until(lambda: len(gateway.calls) >= 3)
            await pilot.pause()

            rows = [entry for entry in app.chat.transcript.children if isinstance(entry, AgentRow)]
            assert rows, "a sub-agent's events never reached the screen"
            assert all(row.children for row in rows)


# ---- approvals as cards -----------------------------------------------------------


@asynctest
async def test_an_approval_is_a_card_and_the_transcript_stays_visible(
    tmp_path: Path, workspace: Path
) -> None:
    """The whole reason it is not a modal: the transcript is what you need in order to
    answer, and a modal covers it."""
    gateway = RecordingGateway(
        [calls_tool("c1", "bash", command="rm -rf build"), says("fine, not doing that")]
    )
    async with Harnessed(tmp_path, workspace, gateway, approval_mode="suggest") as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            assert await until(lambda: card_of(app) is not None)

            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_APPROVE
            assert "rm -rf build" in card._body
            assert app.chat.transcript.children, "the transcript went away"
            assert card.rows[0][0] == "deny", "the safe answer is the highlighted one"

            await pilot.press("n")
            assert await until(lambda: card_of(app) is None)
            assert await until(lambda: len(gateway.calls) >= 2)


@asynctest
async def test_a_long_cell_does_not_push_the_answers_off_the_screen(
    tmp_path: Path, workspace: Path
) -> None:
    """The card is laid out top to bottom, so an unbounded body clips the two rows you
    have to choose between — a question with no visible way to answer it."""
    cell = "\n".join(f"print({index})" for index in range(60))
    gateway = RecordingGateway([calls_tool("c1", "repl", code=cell), says("done")])
    async with Harnessed(tmp_path, workspace, gateway, approval_mode="suggest") as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            assert await until(lambda: card_of(app) is not None)
            await pilot.pause()

            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_APPROVE
            assert "print(0)" in card._body and "print(59)" in card._body
            assert "more lines" in card._body

            zone = app.query_one("#cards")
            options = card.query_one(OptionList)
            assert options.region.height > 0, "the answers are not rendered at all"
            assert (
                options.region.bottom <= zone.region.bottom
            ), "the answers are below the bottom of the card zone"

            await pilot.press("n")
            assert await until(lambda: card_of(app) is None)


# ---- mode, theme, trust -----------------------------------------------------------


@asynctest
async def test_shift_tab_cycles_the_approval_mode(tmp_path: Path, workspace: Path) -> None:
    """From `suggest`, deliberately: the next rung after `auto-edit` is `full-auto`,
    which the daemon refuses on the host — that path is
    `daemon_test.test_set_mode_cannot_reach_full_auto_on_the_host`, not this one."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([]), approval_mode="suggest") as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            assert app.state.effective["mode"] == "suggest"
            await pilot.press("shift+tab")
            assert await until(lambda: app.state.effective["mode"] == "auto-edit")
            # The daemon is authoritative — what the status line shows is what it said.
            runner = env.daemon.sessions[app.state.session_id]
            assert runner.agent.harness.approval_mode == "auto-edit"
            assert load_config(env.config_path).agent.approval_mode == "auto-edit"


@asynctest
async def test_choosing_a_theme_is_remembered(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.chat._run_command("/theme")
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_THEME

            await pilot.press("down", "down", "enter")  # auto -> dark -> light
            await pilot.pause()
            assert app.prefs.theme == "light"
            assert app.theme == "stcode-light"

        from stcode.cli.services.preferences import load_prefs

        assert load_prefs(env.prefs_path).theme == "light"


@asynctest
async def test_an_untrusted_folder_asks_first(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([]), trusted=False) as env:
        app = env.app()
        async with app.run_test() as pilot:
            assert await until(lambda: isinstance(app.screen, TrustScreen))
            assert not app.state.session_id, "nothing was connected before the question"

            await pilot.press("tab", "enter")  # Cancel has focus; move to Trust
            assert await until(lambda: bool(app.state.session_id))
            assert app.prefs.is_trusted(workspace)


@asynctest
async def test_cancelling_the_trust_question_leaves(tmp_path: Path, workspace: Path) -> None:
    """There is no "read-only for now": that is `--mode plan` wearing a disguise."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([]), trusted=False) as env:
        app = env.app()
        async with app.run_test() as pilot:
            assert await until(lambda: isinstance(app.screen, TrustScreen))
            await pilot.press("enter")  # Cancel is focused
            assert await until(lambda: not app.is_running)
        assert not app.prefs.trusted


# ---- clear ------------------------------------------------------------------------


@asynctest
async def test_clear_starts_a_new_session_and_erases_nothing(
    tmp_path: Path, workspace: Path
) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([says("hi")])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            first = app.state.session_id
            await pilot.press("g", "o", "enter")
            assert await until(lambda: bool(env.gateway.calls))
            await pilot.pause()

            app.chat._run_command("/clear")
            assert await until(lambda: app.state.session_id not in ("", first))
            await pilot.pause()

            assert not any(isinstance(e, Message) for e in app.chat.transcript.children)
            # The old transcript is still on disk, which is the point.
            sessions = list((tmp_path / "sessions").glob("*.jsonl"))
            assert [path.stem for path in sessions] == [first]


@asynctest
async def test_the_banner_goes_once_there_is_a_conversation(
    tmp_path: Path, workspace: Path
) -> None:
    """A greeting, not furniture. The test is "does the transcript scroll yet", which
    is the honest reading of "is this long" at any terminal height."""
    gateway = RecordingGateway([says("x" * 4000)])
    async with Harnessed(tmp_path, workspace, gateway) as env:
        app = env.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await connected(app)
            banner = app.query_one("#banner")
            assert banner.display, "an empty transcript should show the wordmark"

            await pilot.press("g", "o", "enter")
            assert await until(lambda: bool(env.gateway.calls))
            assert await until(lambda: not banner.display)


# ---- cards a command opened ------------------------------------------------------


@asynctest
async def test_a_picker_survives_the_input_being_cleared(
    tmp_path: Path, workspace: Path
) -> None:
    """The bug where `/effort` appeared to do nothing at all.

    Submitting clears the input, and clearing posts a `Changed`. That message arrives
    *after* the command has opened its card, so a handler that reads "no token under the
    cursor" as "close whatever is open" closes the card the command just opened — for
    `/effort`, `/theme`, `/mode`, `/mcp` and `/skills` alike. Driven through the keyboard
    rather than `_run_command`, because bypassing the submit path is exactly what hid it.
    """
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            for key in ("slash", "e", "f", "f", "o", "r", "t"):
                await pilot.press(key)
            await pilot.press("enter")  # picks /effort out of the commands card
            await pilot.pause()
            await pilot.pause()

            card = card_of(app)
            assert card is not None, "the effort card was closed by its own submit"
            assert card.kind == labels.CARD_EFFORT
            assert app.chat.prompt.text == ""


@asynctest
async def test_skills_says_so_when_there_are_none(tmp_path: Path, workspace: Path) -> None:
    """An empty answer is still an answer. A card that never appears is not."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            assert await until(lambda: "skills" in app.state.info)
            app.chat._run_command("/skills")
            await pilot.pause()
            await pilot.pause()

            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_SKILLS
            assert card.rows, "the card must say something, even when nothing was found"


# ---- two approvals at once --------------------------------------------------------


@asynctest
async def test_two_parallel_approvals_are_both_answerable(
    tmp_path: Path, workspace: Path
) -> None:
    """The deadlock: one card shown, the second never appearing, the turn parked.

    `CardZone.clear()` removes children on the message pump, so answering the first
    request and immediately asking "is a card open?" got the answer "yes" about a card
    already on its way out — and the queued second request was never shown to anyone.
    """
    gateway = RecordingGateway(
        [
            [
                ToolCallEnd(id="c1", name="bash", input={"command": "rm -rf one"}),
                ToolCallEnd(id="c2", name="bash", input={"command": "rm -rf two"}),
                MessageStop(stop_reason="tool_use", usage=Usage()),
            ],
            says("both done"),
        ]
    )
    async with Harnessed(tmp_path, workspace, gateway, approval_mode="suggest") as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            assert await until(lambda: card_of(app) is not None)
            assert card_of(app).kind == labels.CARD_APPROVE  # type: ignore[union-attr]

            await pilot.press("n")
            # The second one has to arrive on its own. Nothing else will prompt it.
            assert await until(
                lambda: card_of(app) is not None
                and card_of(app).kind == labels.CARD_APPROVE  # type: ignore[union-attr]
            ), "the second approval never reached the screen"

            await pilot.press("n")
            assert await until(lambda: len(gateway.calls) >= 2), "the turn stayed parked"


@asynctest
async def test_backspace_does_not_dismiss_an_approval(
    tmp_path: Path, workspace: Path
) -> None:
    """The hang: backspace closed the card and the turn waited forever.

    By the time an approval is on screen its request has left the queue, so closing the
    card left the session parked on an answer with nowhere to come from — a hung agent
    with a working keyboard. The keystroke was usually backspace on an already-empty
    input, aimed at a character that was not there.
    """
    gateway = RecordingGateway(
        [calls_tool("c1", "bash", command="rm -rf build"), says("fine, not doing that")]
    )
    async with Harnessed(tmp_path, workspace, gateway, approval_mode="suggest") as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            assert await until(lambda: card_of(app) is not None)
            assert card_of(app).kind == labels.CARD_APPROVE  # type: ignore[union-attr]

            for key in ("backspace", "backspace", "escape"):
                await pilot.press(key)
                await pilot.pause()
                card = card_of(app)
                assert card is not None and card.kind == labels.CARD_APPROVE, (
                    f"{key} dismissed the approval and parked the turn"
                )

            # And the only way out still works.
            await pilot.press("n")
            assert await until(lambda: card_of(app) is None)
            assert await until(lambda: len(gateway.calls) >= 2), "the turn stayed parked"


@asynctest
async def test_backspace_still_closes_a_card_that_is_only_offering(
    tmp_path: Path, workspace: Path
) -> None:
    """The rule is "asking, not offering" — a picker must not become sticky too."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("slash")
            await pilot.pause()
            assert card_of(app) is not None
            await pilot.press("backspace")
            await pilot.pause()
            assert card_of(app) is None


# ---- --daemonless: the settings belong to the daemon -------------------------------


async def attached_elsewhere(app: StcodeApp, pilot: Any) -> None:
    """Drive the connect screen, which `--daemonless` opens instead of probing.

    The address it offers is already the right one — it is the launcher's — so pressing
    enter on the focused Connect button is the whole interaction.
    """
    assert await until(lambda: isinstance(app.screen, ConnectScreen)), "no connect screen"
    await pilot.press("enter")
    await connected(app)


@asynctest
async def test_daemonless_shows_the_daemons_config(tmp_path: Path, workspace: Path) -> None:
    """The agent is on the daemon's machine, so its settings are the ones on screen —
    and this terminal has no config file of its own to confuse them with."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app(daemonless=True)
        async with app.run_test() as pilot:
            await attached_elsewhere(app, pilot)
            assert await until(lambda: bool(app.state.config))
            assert app.state.config["path"] == str(env.config_path)
            assert app.state.config["model"] == "fake-large"


@asynctest
async def test_model_writes_the_daemons_config_file(tmp_path: Path, workspace: Path) -> None:
    """`/model` has to outlive the session and the restart, or it is a setting you
    retype every morning. Only the write to the daemon's own `config.toml` survives."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.chat._run_command("/model")
            assert await until(lambda: isinstance(app.screen, SettingsScreen))
            await pilot.pause()  # the screen is up; its fields mount on the next tick
            screen = app.screen
            assert not screen.query_one("#api-key", Input).disabled, "a unix socket takes keys"

            screen.query_one("#model", Input).value = "fake-chosen"
            screen.query_one("#save", Button).press()
            await pilot.pause()

            assert await until(
                lambda: load_config(env.config_path).model.providers["fake"].model == "fake-chosen"
            ), "the daemon's config file never changed"
            assert await until(lambda: app.state.effective.get("model") == "fake-chosen"), (
                "the running session was not pointed at it"
            )


@asynctest
async def test_the_key_field_is_locked_when_the_daemon_takes_no_keys(
    tmp_path: Path, workspace: Path
) -> None:
    """Over TCP the daemon refuses keys; the field says so by being disabled rather than
    by silently not saving."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.push_screen(SettingsScreen({**app.state.config, "keys_editable": False}))
            await pilot.pause()
            assert app.screen.query_one("#api-key", Input).disabled


# ---- ! ----------------------------------------------------------------------------


@asynctest
async def test_a_bang_runs_a_command_here_and_records_nothing(
    tmp_path: Path, workspace: Path
) -> None:
    """`!` is your shell, not a tool call: no approval, no gateway, no session record."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.chat.prompt.text = "!echo hello-from-bang"
            await pilot.press("enter")

            entries = [e for e in app.chat.transcript.children if isinstance(e, ShellOutput)]
            assert len(entries) == 1
            assert await until(lambda: "hello-from-bang" in entries[0]._output)

            assert not env.gateway.calls, "! must never reach the model"
            records = env.daemon.sessions[app.state.session_id].agent.session.records()
            assert not [r for r in records if r.get("type") == "user"]


@asynctest
async def test_a_bang_that_fails_shows_its_exit_code(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.chat.prompt.text = "!exit 3"
            await pilot.press("enter")
            entry = [e for e in app.chat.transcript.children if isinstance(e, ShellOutput)][0]
            assert await until(lambda: entry._exit_code == 3)


# ---- /token -----------------------------------------------------------------------


@asynctest
async def test_token_counts_every_model_call_not_just_the_last(
    tmp_path: Path, workspace: Path
) -> None:
    """A turn with a tool call made two requests. `turn_finished` carries one of them,
    which is why the totals come from the session instead."""
    gateway = RecordingGateway(
        [calls_tool("c1", "read", path="src/app.py"), says("that is the app")]
    )
    async with Harnessed(tmp_path, workspace, gateway) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            assert await until(lambda: len(gateway.calls) >= 2)
            await pilot.pause()

            app.chat._run_command("/token")
            assert await until(
                lambda: card_of(app) is not None
                and card_of(app).kind == labels.CARD_TOKENS  # type: ignore[union-attr]
            )
            rows = dict(card_of(app).rows)  # type: ignore[union-attr]
            assert rows["model calls"] == "2", "only the last call was counted"
            # 20 from the tool-calling turn, 10 from the one that replied.
            assert rows["input"] == "30"


# ---- the working indicator --------------------------------------------------------


@asynctest
async def test_the_status_line_spins_from_the_moment_enter_is_pressed(
    tmp_path: Path, workspace: Path
) -> None:
    """The gap between enter and the first token is the one moment the screen looks
    identical to one where nothing happened."""
    gateway = RecordingGateway([says("eventually")])
    gateway.gate = asyncio.Event()
    async with Harnessed(tmp_path, workspace, gateway) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            await pilot.pause()
            assert app.chat.status.working, "nothing told the user the message had landed"

            gateway.gate.set()
            assert await until(lambda: not app.chat.status.working), "the spinner outlived the turn"


@asynctest
async def test_the_daemon_stream_continues_while_settings_cover_chat(
    tmp_path: Path, workspace: Path
) -> None:
    """A modal must not own or cancel the app's connection worker."""
    gateway = RecordingGateway([says("finished behind the dialog")])
    gateway.gate = asyncio.Event()
    async with Harnessed(tmp_path, workspace, gateway) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("g", "o", "enter")
            await pilot.pause()
            app.chat._run_command("/model")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)
            gateway.gate.set()
            assert await until(lambda: not app.chat.status.working)
            assert any(
                entry.body == "finished behind the dialog"
                for entry in app.chat.transcript.query(Message)
            )
            assert isinstance(app.screen, SettingsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app.chat


# ---- what you said, and what you ran -----------------------------------------------


@asynctest
async def test_your_own_message_is_tinted_and_the_model_s_is_not(
    tmp_path: Path, workspace: Path
) -> None:
    """Scrolling back to find where you asked something should be looking, not reading."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([says("hi")])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("h", "i", "enter")
            assert await until(lambda: len(list(app.chat.transcript.children)) >= 4)
            await pilot.pause()

            mine = next(e for e in app.chat.transcript.children if isinstance(e, Message) and e._role == "user")
            theirs = next(
                e for e in app.chat.transcript.children if isinstance(e, Message) and e._role != "user"
            )
            assert mine.styles.background.a > 0, "the user's message has no tint"
            assert theirs.styles.background.a == 0, "the model's answer must stay plain"


@asynctest
async def test_a_shell_entry_is_marked_by_a_rule_not_a_box(
    tmp_path: Path, workspace: Path
) -> None:
    """It must be impossible to mistake your own command for something the agent did."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.chat.prompt.text = "!true"
            await pilot.press("enter")
            await pilot.pause()
            entry = next(e for e in app.chat.transcript.children if isinstance(e, ShellOutput))
            edge, _colour = entry.styles.border_left
            assert edge == "solid"


# ---- /effort is remembered ---------------------------------------------------------


@asynctest
async def test_choosing_an_effort_reaches_the_session_and_the_file(
    tmp_path: Path, workspace: Path
) -> None:
    """Both halves. The `meta` record is what makes `/effort` mean anything now; the
    config line is what stops it reverting at the next `stcode`."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.chat._run_command("/effort")
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_EFFORT
            # One below the current effort, which is where the card opens.
            await pilot.press("down")
            chosen = card.rows[card._list.highlighted or 0][0]  # noqa: SLF001
            assert chosen != "medium"

            await pilot.press("enter")
            assert await until(
                lambda: env.daemon.sessions[app.state.session_id]
                .agent.session.overrides()
                .get("reasoning_effort")
                == chosen
            ), "the live session never heard about it"

        assert load_config(env.config_path).agent.reasoning_effort == chosen


@asynctest
async def test_a_command_line_flag_is_not_written_to_the_config(
    tmp_path: Path, workspace: Path
) -> None:
    """`stcode --mode plan` once must not leave `plan` in the file — and the UI rewrites
    that file every time the mode changes, which is how the flag used to get in."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([]), approval_mode="suggest") as env:
        app = env.app(mode="plan")
        async with app.run_test() as pilot:
            await connected(app)
            assert app.state.effective["mode"] == "plan", "the flag did not take effect"
            app.chat._run_command("/theme")  # any change that triggers a save
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        assert load_config(env.config_path).agent.approval_mode != "plan", (
            "the flag was written to the config file"
        )


@asynctest
async def test_refused_mode_cycle_warns_and_keeps_connection_usable(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("stcode.core.daemon.autonomy.in_container", lambda: False)
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("shift+tab")
            assert await until(lambda: bool(app.chat.transcript.query(".notice.warning")))
            assert app.state.effective["mode"] == "auto-edit"
            assert not app.chat.transcript.query(".notice.error")
            assert await app.connection.client.info()
            assert load_config(env.config_path).agent.approval_mode == "auto-edit"


@asynctest
async def test_aliases_use_daemon_workspace_when_client_paths_differ(
    tmp_path: Path, workspace: Path
) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        runner = await env.daemon.create_session(cwd=workspace)
        app = build_app(
            Launcher(env.address, log_path=env.log_path), daemonless=True,
            cwd=tmp_path, resume=runner.id, prefs_path=env.prefs_path,
        )
        async with app.run_test() as pilot:
            await attached_elsewhere(app, pilot)
            assert app.options.cwd != workspace
            # Even a stale client snapshot must not change the listing root.
            app.state.info["cwd"] = str(tmp_path)
            app.chat.prompt.text = "!pwd"
            await pilot.press("enter")
            entry = next(iter(app.chat.transcript.query(ShellOutput)))
            assert await until(lambda: str(workspace) in entry._output)
            await pilot.press("at")
            assert await until(lambda: bool(app.chat.files))
            assert "src/app.py" in app.chat.files
            entry.finish("\n".join(str(n) for n in range(80)), 0)
            await pilot.pause()
            assert entry.size.height == 6
            assert entry.max_scroll_y > 0


@asynctest
async def test_file_completion_after_disconnect_warns_without_closing_ui(
    tmp_path: Path, workspace: Path
) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await app.connection.close_client()
            await pilot.press("at")
            assert await until(lambda: bool(app.chat.transcript.query(".notice.warning")))
            assert app.chat.files == []

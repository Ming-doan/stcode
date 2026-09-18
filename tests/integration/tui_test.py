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
from typing import Any, Callable

from conftest import asynctest
from fakes import RecordingGateway, calls_tool, says

from stcode.cli import labels
from stcode.cli.app import StcodeApp
from stcode.cli.cards import Card
from stcode.cli.modals import TrustScreen
from stcode.cli.prefs import UiPrefs, save_prefs
from stcode.cli.transcript import AgentRow, Message, ShellOutput, ToolCall
from stcode.core.configs import GatewayConfig, save_config
from stcode.core.daemon import Daemon
from stcode.core.providers.types import MessageStop, ToolCallEnd, Usage

CONFIG = """\
[defaults]
provider = "fake"
model = "fake-large"
approval_mode = "auto-edit"

[providers.fake]
api_key = "test-key"

[routing.high]
provider = "fake"
model = "fake-large"

[routing.medium]
provider = "fake"
model = "fake-small"

[routing.low]
provider = "fake"
model = "fake-small"

[supervisor]
enabled = false

[mcp]
enabled = false
"""


class Harnessed:
    """A daemon on a unix socket in `tmp_path`, plus an app pointed at it."""

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

        settings = GatewayConfig()
        settings.session.dir = str(tmp_path / "sessions")
        settings.daemon.socket = str(tmp_path / "d.sock")
        settings.defaults.approval_mode = approval_mode  # type: ignore[assignment]
        # The app reads the file; the daemon is handed the object. Same socket, same
        # session directory — which is the whole contract between them.
        save_config(_merge(settings, self.config_path), self.config_path)
        self.settings = settings
        self.daemon = Daemon(settings, gateway=gateway)  # type: ignore[arg-type]
        self.gateway = gateway
        self.workspace = workspace

    def app(self, **kwargs: Any) -> StcodeApp:
        return StcodeApp(
            self.config_path,
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


def _merge(settings: GatewayConfig, path: Path) -> GatewayConfig:
    """The test config file, with the socket and session directory of this run."""
    import tomllib

    with path.open("rb") as handle:
        data = tomllib.load(handle)
    merged = GatewayConfig.model_validate(data)
    merged.session.dir = settings.session.dir
    merged.daemon.socket = settings.daemon.socket
    merged.defaults.approval_mode = settings.defaults.approval_mode
    merged.supervisor.enabled = False
    merged.mcp.enabled = False
    return merged


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def connected(app: StcodeApp) -> None:
    assert await until(lambda: bool(app._session_id)), "the app never opened a session"


def card_of(app: StcodeApp) -> Card | None:
    return app.cards.current


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
            assert app.prompt.text == "", "the trigger character is not inserted"
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
            assert app.prompt.text == ""

            await pilot.press("question_mark", "question_mark")
            await pilot.pause()
            assert app.prompt.text == "?"
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
            # offer — it would move the terminal off the daemon it just started.
            assert len(card.rows) == len(labels.COMMANDS) - 1
            assert "/connect" not in [row[0] for row in card.rows]

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
            assert app.prompt.text == "read src/a"
            assert card_of(app) is None


@asynctest
async def test_an_at_sign_offers_the_workspace_files(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            await pilot.press("at")
            await pilot.pause()
            assert await until(lambda: bool(app._files))
            await pilot.press("a", "p", "p")
            await pilot.pause()

            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_FILES
            assert any("app.py" in row[0] for row in card.rows)

            await pilot.press("enter")
            await pilot.pause()
            assert "app.py" in app.prompt.text
            assert card_of(app) is None


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
            assert app.prompt.text == "hi\nthere"
            assert not env.gateway.calls, "shift+enter must not send"

            await pilot.press("enter")
            await pilot.pause()
            assert app.prompt.text == ""
            assert await until(lambda: bool(env.gateway.calls))
            assert [message.role for message in env.gateway.last.messages] == ["user"]
            assert any(isinstance(entry, Message) for entry in app.transcript.children)


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

            boxes = [entry for entry in app.transcript.children if isinstance(entry, ToolCall)]
            assert len(boxes) == 1
            assert app._tools == {}, "the box was matched to its result and released"


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

            rows = [entry for entry in app.transcript.children if isinstance(entry, AgentRow)]
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
            assert app.transcript.children, "the transcript went away"
            assert card.rows[0][0] == "deny", "the safe answer is the highlighted one"

            await pilot.press("n")
            assert await until(lambda: card_of(app) is None)
            assert await until(lambda: len(gateway.calls) >= 2)


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
            before = app.config.defaults.approval_mode
            await pilot.press("shift+tab")
            assert await until(lambda: app.config.defaults.approval_mode != before)
            # The daemon is authoritative — what the status line shows is what it said.
            runner = env.daemon.sessions[app._session_id]
            assert runner.agent.harness.approval_mode == app.config.defaults.approval_mode


@asynctest
async def test_choosing_a_theme_is_remembered(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app._run_command("/theme")
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_THEME

            await pilot.press("down", "down", "enter")  # auto -> dark -> light
            await pilot.pause()
            assert app.prefs.theme == "light"
            assert app.theme == "stcode-light"

        from stcode.cli.prefs import load_prefs

        assert load_prefs(env.prefs_path).theme == "light"


@asynctest
async def test_an_untrusted_folder_asks_first(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([]), trusted=False) as env:
        app = env.app()
        async with app.run_test() as pilot:
            assert await until(lambda: isinstance(app.screen, TrustScreen))
            assert not app._session_id, "nothing was connected before the question"

            await pilot.press("tab", "enter")  # Cancel has focus; move to Trust
            assert await until(lambda: bool(app._session_id))
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
            first = app._session_id
            await pilot.press("g", "o", "enter")
            assert await until(lambda: bool(env.gateway.calls))
            await pilot.pause()

            app._run_command("/clear")
            assert await until(lambda: app._session_id not in ("", first))
            await pilot.pause()

            assert not any(isinstance(e, Message) for e in app.transcript.children)
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
            assert app.prompt.text == ""


@asynctest
async def test_skills_says_so_when_there_are_none(tmp_path: Path, workspace: Path) -> None:
    """An empty answer is still an answer. A card that never appears is not."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            assert await until(lambda: "skills" in app._info)
            app._run_command("/skills")
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
            app.prompt.text = "!echo hello-from-bang"
            await pilot.press("enter")

            entries = [e for e in app.transcript.children if isinstance(e, ShellOutput)]
            assert len(entries) == 1
            assert await until(lambda: "hello-from-bang" in entries[0]._output)

            assert not env.gateway.calls, "! must never reach the model"
            records = env.daemon.sessions[app._session_id].agent.session.records()
            assert not [r for r in records if r.get("type") == "user"]


@asynctest
async def test_a_bang_that_fails_shows_its_exit_code(tmp_path: Path, workspace: Path) -> None:
    async with Harnessed(tmp_path, workspace, RecordingGateway([])) as env:
        app = env.app()
        async with app.run_test() as pilot:
            await connected(app)
            app.prompt.text = "!exit 3"
            await pilot.press("enter")
            entry = [e for e in app.transcript.children if isinstance(e, ShellOutput)][0]
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

            app._run_command("/token")
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
            assert app._working, "nothing told the user the message had landed"

            gateway.gate.set()
            assert await until(lambda: not app._working), "the spinner outlived the turn"


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
            assert await until(lambda: len(list(app.transcript.children)) >= 4)
            await pilot.pause()

            mine = next(e for e in app.transcript.children if isinstance(e, Message) and e._role == "user")
            theirs = next(
                e for e in app.transcript.children if isinstance(e, Message) and e._role != "user"
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
            app.prompt.text = "!true"
            await pilot.press("enter")
            await pilot.pause()
            entry = next(e for e in app.transcript.children if isinstance(e, ShellOutput))
            edge, _colour = entry.styles.border_left
            assert edge == "thick"


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
            app._run_command("/effort")
            await pilot.pause()
            card = card_of(app)
            assert card is not None and card.kind == labels.CARD_EFFORT
            chosen = card.rows[0][0]

            await pilot.press("enter")
            assert await until(
                lambda: env.daemon.sessions[app._session_id]
                .agent.session.overrides()
                .get("reasoning_effort")
                == chosen
            ), "the live session never heard about it"

        from stcode.core.configs import load_config

        assert load_config(env.config_path).defaults.reasoning_effort == chosen


@asynctest
async def test_a_command_line_flag_is_not_written_to_the_config(
    tmp_path: Path, workspace: Path
) -> None:
    """`stcode --mode plan` once must not leave `plan` in the file — and the UI rewrites
    that file every time the mode changes, which is how the flag used to get in."""
    async with Harnessed(tmp_path, workspace, RecordingGateway([]), approval_mode="suggest") as env:
        app = env.app(overrides={"approval_mode": "plan"})
        async with app.run_test() as pilot:
            await connected(app)
            assert app.config.defaults.approval_mode == "plan", "the flag did not take effect"
            app._run_command("/theme")  # any change that triggers a save
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        from stcode.core.configs import load_config

        assert load_config(env.config_path).defaults.approval_mode != "plan", (
            "the flag was written to the config file"
        )

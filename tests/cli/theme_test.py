"""
Theme tests — following the terminal, and never hanging while trying to.

Textual 8 has no terminal-background detection of its own, so `auto` is ours: read
`COLORFGBG` when the terminal exports one, otherwise ask with `OSC 11` on a short
budget, otherwise dark.

The failure that matters is not "picked the wrong theme" — it is a chat UI that never
starts because a terminal did not answer a question nobody asked it to.
"""

from __future__ import annotations

from stcode.cli.theme import (
    PRIMARY,
    STCODE_DARK,
    STCODE_LIGHT,
    background_is_light,
    brand_text,
    mode_from_colorfgbg,
    theme_name_for,
)


def test_both_themes_carry_the_project_colour() -> None:
    assert PRIMARY == "#78b032"
    assert STCODE_DARK.primary == PRIMARY and STCODE_LIGHT.primary == PRIMARY
    assert STCODE_DARK.dark and not STCODE_LIGHT.dark


def test_a_light_background_reply_is_read_as_light() -> None:
    assert background_is_light("\x1b]11;rgb:ffff/ffff/ffff\x07") is True
    assert background_is_light("\x1b]11;rgb:1c1c/1c1c/1c1c\x07") is False


def test_luminance_not_brightness_decides() -> None:
    """A saturated blue background is dark even though one channel is at full."""
    assert background_is_light("\x1b]11;rgb:0000/0000/ffff") is False
    # ...and a mid yellow is light, because green dominates perceived luminance.
    assert background_is_light("\x1b]11;rgb:dddd/dddd/0000") is True


def test_eight_and_sixteen_bit_replies_both_parse() -> None:
    assert background_is_light("\x1b]11;rgb:ff/ff/ff\x07") is True
    assert background_is_light("\x1b]11;rgb:00/00/00\x07") is False


def test_a_reply_that_is_not_one_is_not_an_answer() -> None:
    for junk in ("", "\x1b]11;?\x07", "nonsense", "\x1b]11;rgb:zz/zz/zz\x07"):
        assert background_is_light(junk) is None


def test_colorfgbg_is_read_when_the_terminal_exports_one() -> None:
    assert mode_from_colorfgbg("15;0") == "dark"
    assert mode_from_colorfgbg("0;15") == "light"
    assert mode_from_colorfgbg("0;default") == "light"
    assert mode_from_colorfgbg("") is None
    assert mode_from_colorfgbg("nonsense") is None


def test_an_explicit_preference_beats_whatever_the_terminal_says() -> None:
    assert theme_name_for("dark", detected="light") == STCODE_DARK.name
    assert theme_name_for("light", detected="dark") == STCODE_LIGHT.name
    assert theme_name_for("auto", detected="light") == STCODE_LIGHT.name
    assert theme_name_for("auto", detected="dark") == STCODE_DARK.name


def test_brand_text_is_readable_in_both_themes() -> None:
    """`primary` is the project's green in both themes, as the spec says. Text is a
    separate variable, because #78b032 on an off-white ground is about 2.6:1 — fine for
    a border, unreadable as a sentence."""
    from rich.color import Color as RichColor

    def luminance(hex_colour: str) -> float:
        red, green, blue = RichColor.parse(hex_colour).get_truecolor()

        def channel(value: int) -> float:
            fraction = value / 255
            return fraction / 12.92 if fraction <= 0.03928 else ((fraction + 0.055) / 1.055) ** 2.4

        return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

    def contrast(one: str, two: str) -> float:
        first, second = sorted((luminance(one), luminance(two)), reverse=True)
        return (first + 0.05) / (second + 0.05)

    for theme in (STCODE_DARK, STCODE_LIGHT):
        assert theme.primary == PRIMARY
        background = theme.background
        assert background is not None
        assert contrast(brand_text(theme.dark), background) >= 4.5

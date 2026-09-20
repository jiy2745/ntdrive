"""The VNC keysym mapping: tokens, shifted characters, modifier combos."""

from __future__ import annotations

from ntdrive.screen import keymap

SHIFT = 0xFFE1
CTRL = 0xFFE3
ALT = 0xFFE9
ENTER = 0xFF0D


def test_lowercase_char_is_one_press_and_release() -> None:
    assert keymap.key_strokes(["a"]) == [[(0x61, True), (0x61, False)]]


def test_uppercase_holds_shift() -> None:
    assert keymap.key_strokes(["A"]) == [
        [(SHIFT, True), (0x41, True), (0x41, False), (SHIFT, False)]
    ]


def test_shifted_symbol_holds_shift() -> None:
    # '!' keysym is its code point 0x21, typed with Shift held.
    assert keymap.key_strokes(["!"]) == [
        [(SHIFT, True), (0x21, True), (0x21, False), (SHIFT, False)]
    ]


def test_named_token() -> None:
    assert keymap.key_strokes(["{enter}"]) == [[(ENTER, True), (ENTER, False)]]


def test_ctrl_combo_wraps_the_key() -> None:
    assert keymap.key_strokes(["{ctrl+c}"]) == [
        [(CTRL, True), (0x63, True), (0x63, False), (CTRL, False)]
    ]


def test_multi_modifier_combo_in_order() -> None:
    # ctrl+alt+delete: hold ctrl, hold alt, press delete, release alt, release ctrl.
    assert keymap.key_strokes(["{ctrl+alt+delete}"]) == [
        [
            (CTRL, True),
            (ALT, True),
            (0xFFFF, True),
            (0xFFFF, False),
            (ALT, False),
            (CTRL, False),
        ]
    ]


def test_text_and_tokens_mix() -> None:
    strokes = keymap.key_strokes(["hi", "{enter}"])
    assert strokes == [
        [(0x68, True), (0x68, False)],
        [(0x69, True), (0x69, False)],
        [(ENTER, True), (ENTER, False)],
    ]


def test_unknown_token_is_typed_literally() -> None:
    # An unrecognized token is typed as its characters, matching the terminal.
    typed = [
        ks for ks, down in keymap.flatten(keymap.key_strokes(["{nope}"])) if down and ks < 0x7F
    ]
    assert "".join(chr(k) for k in typed) == "{nope}"


def test_literal_strokes_never_parse_tokens() -> None:
    # A password of literal braces is typed, not read as a token.
    typed = [
        ks for ks, down in keymap.flatten(keymap.literal_strokes("a{b}")) if down and ks < 0x7F
    ]
    assert "".join(chr(k) for k in typed) == "a{b}"


def test_flatten_concatenates_events() -> None:
    assert keymap.flatten([[(1, True)], [(2, False)]]) == [(1, True), (2, False)]

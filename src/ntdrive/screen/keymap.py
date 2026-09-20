"""Key tokens such as {enter} and {ctrl+alt+delete} and the X keysym events VNC needs.

The token vocabulary is the same one the terminal uses (`ntdrive.term.keys`), so an agent learns
it once. The terminal turns a token into a byte sequence for a PTY. VNC instead needs a stream of
KeyEvent messages, each an X11 keysym pressed down and released up, with modifiers held around a
key. This module turns a list of text or `{token}` items into that event stream.
"""

from __future__ import annotations

import re

# X keysyms for the named keys. Printable ASCII uses its own code point as the keysym.
NAMED_KEYSYMS: dict[str, int] = {
    "enter": 0xFF0D,
    "return": 0xFF0D,
    "tab": 0xFF09,
    "esc": 0xFF1B,
    "escape": 0xFF1B,
    "backspace": 0xFF08,
    "space": 0x20,
    "delete": 0xFFFF,
    "insert": 0xFF63,
    "up": 0xFF52,
    "down": 0xFF54,
    "left": 0xFF51,
    "right": 0xFF53,
    "home": 0xFF50,
    "end": 0xFF57,
    "pgup": 0xFF55,
    "pgdn": 0xFF56,
    "win": 0xFFEB,
    "menu": 0xFF67,
    **{f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)},
}

# Modifier keysyms, held down around the key of a combo like {ctrl+c} or {win+r}.
MODIFIERS: dict[str, int] = {
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "alt": 0xFFE9,
    "shift": 0xFFE1,
    "win": 0xFFEB,
    "super": 0xFFEB,
}
SHIFT = 0xFFE1

# ASCII characters that need Shift held on a US keyboard. Their keysym is still the code point.
_SHIFTED = set('~!@#$%^&*()_+{}|:"<>?') | {chr(c) for c in range(ord("A"), ord("Z") + 1)}

# One {token}, the same shape the terminal accepts.
TOKEN = re.compile(r"\{([a-zA-Z0-9+_-]+)\}")

# One stroke is the events for one character or one token: modifier and key presses paired with
# their releases. Each event is (keysym, down).
Stroke = list[tuple[int, bool]]


def _char_stroke(ch: str) -> Stroke | None:
    """Press and release one character, Shift held when needed. None when not printable ASCII."""
    if ch == "\n":
        return [(0xFF0D, True), (0xFF0D, False)]
    if ch == "\t":
        return [(0xFF09, True), (0xFF09, False)]
    code = ord(ch)
    if not 0x20 <= code <= 0x7E:
        return None
    if ch in _SHIFTED:
        return [(SHIFT, True), (code, True), (code, False), (SHIFT, False)]
    return [(code, True), (code, False)]


def _token_stroke(token: str) -> Stroke | None:
    """One stroke for a `{token}` (without braces), or None when the token is not recognized.

    A token is `mod+mod+key`: the modifiers are held down in order, the key is pressed and
    released, then the modifiers are released in reverse. Case does not matter (`{ctrl+C}` is
    `{ctrl+c}`).
    """
    *mods, key = token.split("+")
    if not key:
        return None
    holds: list[int] = []
    for mod in mods:
        keysym = MODIFIERS.get(mod.lower())
        if keysym is None:
            return None
        holds.append(keysym)
    low = key.lower()
    if low in NAMED_KEYSYMS:
        key_sym, key_shift = NAMED_KEYSYMS[low], False
    elif len(key) == 1 and _char_stroke(key) is not None:
        key_sym, key_shift = ord(key), key in _SHIFTED
    else:
        return None
    if key_shift and SHIFT not in holds:
        holds.append(SHIFT)
    events: Stroke = [(keysym, True) for keysym in holds]
    events += [(key_sym, True), (key_sym, False)]
    events += [(keysym, False) for keysym in reversed(holds)]
    return events


def _text_strokes(text: str) -> list[Stroke]:
    """Strokes for one item: `{tokens}` expanded, other characters typed, unknown tokens literal."""
    strokes: list[Stroke] = []
    pos = 0
    for match in TOKEN.finditer(text):
        strokes.extend(s for ch in text[pos : match.start()] if (s := _char_stroke(ch)))
        token = _token_stroke(match.group(1))
        if token is not None:
            strokes.append(token)
        else:  # an unrecognized token is typed literally, as the terminal does
            strokes.extend(s for ch in match.group(0) if (s := _char_stroke(ch)))
        pos = match.end()
    strokes.extend(s for ch in text[pos:] if (s := _char_stroke(ch)))
    return strokes


def key_strokes(items: list[str]) -> list[Stroke]:
    """Strokes for a burst of keys, each item being text or a single `{token}`."""
    strokes: list[Stroke] = []
    for item in items:
        strokes.extend(_text_strokes(item))
    return strokes


def literal_strokes(text: str) -> list[Stroke]:
    """Strokes that type `text` verbatim, with no `{token}` parsing.

    For a secret the daemon expands itself (a password read from vms.yaml): braces in it stay
    literal and it never passes through the token scanner or a tool argument.
    """
    return [s for ch in text if (s := _char_stroke(ch))]


def flatten(strokes: list[Stroke]) -> list[tuple[int, bool]]:
    """The strokes as one flat list of (keysym, down) events for the wire."""
    return [event for stroke in strokes for event in stroke]

"""Key tokens such as {ctrl+c} and {enter} and their terminal byte sequences."""

from __future__ import annotations

import re

NAMED_KEYS: dict[str, bytes] = {
    "enter": b"\r",
    "return": b"\r",
    "tab": b"\t",
    "esc": b"\x1b",
    "escape": b"\x1b",
    "backspace": b"\x7f",
    "space": b" ",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pgup": b"\x1b[5~",
    "pgdn": b"\x1b[6~",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}

TOKEN = re.compile(r"\{([a-zA-Z0-9+_-]+)\}")


def token_bytes(token: str) -> bytes | None:
    """Bytes for a single token name without braces, or None when unknown."""
    name = token.lower()
    if name in NAMED_KEYS:
        return NAMED_KEYS[name]
    if name.startswith("ctrl+") and len(name) == 6:
        ch = name[5]
        if "a" <= ch <= "z":
            return bytes([ord(ch) - ord("a") + 1])
        if ch == "[":
            return b"\x1b"
        if ch == "]":
            return b"\x1d"
        if ch == "\\":
            return b"\x1c"
    if name.startswith("alt+") and len(name) == 5:
        return b"\x1b" + name[4].encode()
    return None


def encode_keys(text: str) -> bytes:
    """Turn a string with {tokens} into bytes; unknown tokens are sent literally."""
    out = bytearray()
    pos = 0
    for match in TOKEN.finditer(text):
        out += text[pos : match.start()].encode("utf-8")
        seq = token_bytes(match.group(1))
        out += seq if seq is not None else match.group(0).encode("utf-8")
        pos = match.end()
    out += text[pos:].encode("utf-8")
    return bytes(out)


def encode_key_list(keys: list[str]) -> bytes:
    """Encode a burst of keys, each item being text or a single {token}."""
    return b"".join(encode_keys(k) for k in keys)

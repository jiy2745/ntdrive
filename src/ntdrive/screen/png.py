"""Just enough PNG reading to tell a real frame from a blank one."""

from __future__ import annotations

import zlib
from pathlib import Path

_MAGIC = b"\x89PNG\r\n\x1a\n"


def is_blank_png(path: str) -> bool:
    """True when every byte of the PNG's image data is zero, which is a solid black frame.

    `vmrun captureScreen` returns a valid all-black PNG when the guest has no interactive session
    (pre-login, WinRE, early boot) and still reports success, so the caller cannot tell it from a
    real screenshot. No image library is needed to spot it: a uniform image filters to zeros
    whatever PNG filter type each scanline used, so an all-zero inflate of the IDAT stream is a
    reliable test. Anything unparseable answers False, because a frame that cannot be checked is
    not known to be blank.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return False
    if not raw.startswith(_MAGIC):
        return False
    idat = bytearray()
    pos = len(_MAGIC)
    while pos + 8 <= len(raw):
        length = int.from_bytes(raw[pos : pos + 4], "big")
        kind = raw[pos + 4 : pos + 8]
        if kind == b"IEND":
            break
        if kind == b"IDAT":
            idat += raw[pos + 8 : pos + 8 + length]
        pos += 12 + length  # length, type, body, crc
    if not idat:
        return False
    try:
        return not any(zlib.decompress(bytes(idat)))
    except zlib.error:
        return False

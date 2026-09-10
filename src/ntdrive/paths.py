"""Host path helpers shared by the clients and the file tools.

The daemon is a separate process with its own working directory, so a relative host path means
nothing once it crosses the wire. Clients absolutize before sending and the tools refuse what is
still relative.
"""

from __future__ import annotations

import os

_SEPARATORS = ("\\", "/")


def absolutize_local(value: str) -> str:
    """Absolute form of a host path that keeps a trailing separator.

    `file_pull` treats `out/` as a directory and `out` as a file, and `os.path.abspath` drops the
    trailing separator, so it is restored here.
    """
    out = os.path.abspath(value)
    if value.endswith(_SEPARATORS) and not out.endswith(_SEPARATORS):
        out += os.sep
    return out


def is_absolute_local(value: str) -> bool:
    """True for a drive-letter, UNC or root-anchored host path."""
    return os.path.isabs(value) or value.startswith(("\\\\", "//"))


__all__ = ["absolutize_local", "is_absolute_local"]

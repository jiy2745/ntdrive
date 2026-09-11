"""Read the key = value pairs of a VMware vmx file."""

from __future__ import annotations

import re
from pathlib import Path

_PAIR = re.compile(r'(?m)^\s*([^#=\s]+)\s*=\s*"(.*)"\s*$')


def vmx_settings(vmx: str) -> dict[str, str]:
    """The pairs of a vmx with lower-cased keys, or {} when the file cannot be read."""
    try:
        text = Path(vmx).read_text(encoding="latin-1")
    except OSError:
        return {}
    return {m.group(1).lower(): m.group(2) for m in _PAIR.finditer(text)}

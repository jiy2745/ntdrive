"""Read the key = value pairs of a VMware vmx file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PAIR = re.compile(r'(?m)^\s*([^#=\s]+)\s*=\s*"(.*)"\s*$')


def vmx_settings(vmx: str) -> dict[str, str]:
    """The pairs of a vmx with lower-cased keys, or {} when the file cannot be read."""
    try:
        text = Path(vmx).read_text(encoding="latin-1")
    except OSError:
        return {}
    return {m.group(1).lower(): m.group(2) for m in _PAIR.finditer(text)}


# One line of a vmx: key = "value". Keys are matched without regard to case, as VMware does.
_LINE = re.compile(r'^\s*([\w.:]+)\s*=\s*"(.*)"\s*$')
# Hardware exposed by vm_config and the vmx keys behind it. cpus writes both numvcpus and
# cpuid.coresPerSocket, so the guest sees one socket with that many cores: Windows client
# editions accept at most one or two sockets and would ignore the rest.
NIC_MODELS = ("e1000e", "e1000", "vmxnet3")


def hardware_from_settings(settings: dict[str, str]) -> dict[str, Any]:
    """cpus, cores_per_socket, memory_mb and nic from lower-cased vmx pairs (None when absent)."""

    def as_int(key: str) -> int | None:
        try:
            return int(settings.get(key, ""))
        except ValueError:
            return None

    return {
        "cpus": as_int("numvcpus"),
        "cores_per_socket": as_int("cpuid.corespersocket"),
        "memory_mb": as_int("memsize"),
        "nic": settings.get("ethernet0.virtualdev") or None,
    }


def apply_hardware(text: str, changes: dict[str, Any]) -> tuple[str, list[str]]:
    """The vmx text with `changes` (cpus, memory_mb, nic) applied, and the vmx keys that changed.

    Existing lines are rewritten in place, whatever their key casing, missing ones are appended,
    and every other line is left byte for byte as it was. An unchanged value counts as no change.
    """
    values: dict[str, str] = {}
    if "cpus" in changes:
        values["numvcpus"] = str(changes["cpus"])
        values["cpuid.coresPerSocket"] = str(changes["cpus"])
    if "memory_mb" in changes:
        values["memsize"] = str(changes["memory_mb"])
    if "nic" in changes:
        values["ethernet0.virtualDev"] = str(changes["nic"])
    wanted = {key.lower(): (key, value) for key, value in values.items()}
    out: list[str] = []
    seen: set[str] = set()
    changed: list[str] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and m.group(1).lower() in wanted:
            key, value = wanted[m.group(1).lower()]
            seen.add(key.lower())
            if m.group(2) != value:
                changed.append(key)
                line = f'{m.group(1)} = "{value}"'
        out.append(line)
    for lowered, (key, value) in wanted.items():
        if lowered not in seen:
            out.append(f'{key} = "{value}"')
            changed.append(key)
    return "\n".join(out) + "\n", changed

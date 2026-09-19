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

# The lines a suspend writes. Workstation restores the named file on the next start.
SAVED_STATE_KEYS = ("checkpoint.vmstate", "checkpoint.vmstate.readonly")


def saved_state(settings: dict[str, str]) -> str:
    """The saved-state file the vmx names (checkpoint.vmState), or an empty string."""
    return settings.get("checkpoint.vmstate", "")


def saved_state_is_stale(vmx: str, settings: dict[str, str]) -> bool:
    """True when the vmx names a saved state that is not a .vmss next to it.

    A suspend writes `checkpoint.vmState = "<name>.vmss"`. A snapshot taken while the VM is
    suspended can leave the key pointing at the snapshot's `.vmsn` instead, and `vmrun start`
    then fails with "The operation was canceled" (seen live on 2026-09-18 on an encrypted VM
    after the allow_suspend path). Dropping the lines boots the VM fresh from its disk.
    """
    name = saved_state(settings)
    if not name:
        return False
    path = Path(vmx).parent / name
    return path.suffix.lower() != ".vmss" or not path.is_file()


def drop_saved_state(text: str) -> tuple[str, list[str]]:
    """The vmx text without the checkpoint.vmState lines, and the lines that were removed."""
    out: list[str] = []
    removed: list[str] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and m.group(1).lower() in SAVED_STATE_KEYS:
            removed.append(line.strip())
            continue
        out.append(line)
    return "\n".join(out) + "\n", removed


# Hardware exposed by vm_config and the vmx keys behind it. cpus writes both numvcpus and
# cpuid.coresPerSocket, so the guest sees one socket with that many cores: Windows client
# editions accept at most one or two sockets and would ignore the rest.
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

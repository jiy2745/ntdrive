"""Interface every hypervisor backend implements.

The tools only talk to this interface. VMware is the one implementation shipped now; Hyper-V and
VirtualBox adapters are planned for a later version and would plug in here unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ntdrive.config import VmConfig
from ntdrive.core.state import PowerState
from ntdrive.errors import BACKEND_UNSUPPORTED, NtDriveError


@dataclass
class SnapshotNode:
    """One snapshot and its children."""

    name: str
    children: list[SnapshotNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Wire form."""
        return {"name": self.name, "children": [c.to_dict() for c in self.children]}


@dataclass
class SnapshotTree:
    """Snapshot forest plus the snapshot the VM currently descends from."""

    roots: list[SnapshotNode] = field(default_factory=list)
    current: str | None = None

    def names(self) -> list[str]:
        """Flat list of snapshot names in tree order."""
        out: list[str] = []

        def walk(nodes: list[SnapshotNode]) -> None:
            for node in nodes:
                out.append(node.name)
                walk(node.children)

        walk(self.roots)
        return out

    def to_dict(self) -> dict[str, Any]:
        """Wire form."""
        return {"tree": [r.to_dict() for r in self.roots], "current": self.current}


class HypervisorAdapter(ABC):
    """Power, snapshot, console and file operations for one backend."""

    backend: str = "abstract"

    @abstractmethod
    def capabilities(self) -> list[str]:
        """Feature flags such as `screenshot`, `send_keys`, `copy_file`, `run_in_guest`."""

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        """Binary path, version and reachability of the backend."""

    @abstractmethod
    async def power_state(self, vm: VmConfig) -> PowerState:
        """Current power state."""

    @abstractmethod
    async def start(self, vm: VmConfig, gui: bool = False) -> None:
        """Power on or resume."""

    @abstractmethod
    async def stop(self, vm: VmConfig, hard: bool = False) -> None:
        """Shut down (soft, needs guest tools) or cut power (hard)."""

    @abstractmethod
    async def reset(self, vm: VmConfig, hard: bool = True) -> None:
        """Reboot from the hypervisor side."""

    @abstractmethod
    async def suspend(self, vm: VmConfig) -> None:
        """Suspend to disk."""

    @abstractmethod
    async def snapshot_take(self, vm: VmConfig, name: str) -> None:
        """Take a snapshot; includes memory when the VM is running."""

    @abstractmethod
    async def snapshot_list(self, vm: VmConfig) -> SnapshotTree:
        """Snapshot tree and current position."""

    @abstractmethod
    async def snapshot_revert(self, vm: VmConfig, name: str) -> None:
        """Revert. The VM may be left powered off or suspended; callers normalize with start()."""

    @abstractmethod
    async def snapshot_delete(self, vm: VmConfig, name: str, children: bool = False) -> None:
        """Delete a snapshot, optionally with its subtree."""

    @abstractmethod
    async def guest_ip(self, vm: VmConfig, timeout: float = 60.0) -> str:
        """IPv4 address of the guest as reported by guest tools."""

    @abstractmethod
    async def screenshot(self, vm: VmConfig, out_path: str) -> str:
        """Save a console screenshot as PNG and return the path."""

    @abstractmethod
    async def copy_to_guest(self, vm: VmConfig, local: str, remote: str) -> None:
        """Copy a file into the guest through guest tools (fallback path)."""

    @abstractmethod
    async def copy_from_guest(self, vm: VmConfig, remote: str, local: str) -> None:
        """Copy a file out of the guest through guest tools (fallback path)."""

    @abstractmethod
    async def run_in_guest(self, vm: VmConfig, program: str, args: list[str]) -> None:
        """Run a program inside the guest without waiting (used for soft reboot)."""

    async def ensure_serial_pipe(self, vm: VmConfig, pipe: str) -> bool:
        """Expose a guest COM port as a host named pipe for serial KD. True when changed."""
        raise NtDriveError(
            BACKEND_UNSUPPORTED, f"backend {self.backend} cannot configure a serial pipe"
        )

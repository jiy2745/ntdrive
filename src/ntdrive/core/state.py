"""In-memory state shared by every tool: VM power, debugger state, terminal sessions.

The store is the single truth inside the daemon. Adapters and sessions push changes here; tools
read from here before touching a VM so that, for example, a terminal call is refused while the
debugger holds the guest frozen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PowerState(StrEnum):
    """Hypervisor power state."""

    OFF = "off"
    RUNNING = "running"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class KdState(StrEnum):
    """Kernel debugger session state."""

    DETACHED = "detached"
    WAITING = "waiting"
    RUNNING = "running"
    BROKEN = "broken"


class TermState(StrEnum):
    """Terminal session state."""

    OPEN = "open"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"


@dataclass
class TermInfo:
    """What the state store knows about a terminal session."""

    session_id: str
    vm: str
    shell: str
    transport: str
    state: TermState = TermState.OPEN
    opened_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    successor: str | None = None
    coview_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Wire form."""
        return {
            "session_id": self.session_id,
            "vm": self.vm,
            "shell": self.shell,
            "transport": self.transport,
            "state": str(self.state),
            "opened_at": self.opened_at,
            "last_activity": self.last_activity,
            "successor": self.successor,
            "coview_url": self.coview_url,
        }


@dataclass
class VmRuntime:
    """Runtime facts about one VM that are not derivable from the hypervisor."""

    name: str
    power: PowerState = PowerState.UNKNOWN
    kd_state: KdState = KdState.DETACHED
    kd_transport: str = "net"
    kd_port: int | None = None
    kd_serial_pipe: str | None = None
    kd_target_info: str = ""
    kd_last_event: dict[str, Any] | None = None
    kd_log_path: str = ""
    current_snapshot: str | None = None
    terms: dict[str, TermInfo] = field(default_factory=dict)
    last_event: dict[str, Any] | None = None

    @property
    def guest_frozen(self) -> bool:
        """True while the debugger holds the target at a prompt."""
        return self.kd_state == KdState.BROKEN

    def open_terms(self) -> list[TermInfo]:
        """Sessions that are still connected."""
        return [t for t in self.terms.values() if t.state == TermState.OPEN]

    def to_dict(self) -> dict[str, Any]:
        """Wire form used by sys_state and vm_state."""
        return {
            "name": self.name,
            "power": str(self.power),
            "kd": {
                "state": str(self.kd_state),
                "transport": self.kd_transport,
                "port": self.kd_port,
                "serial_pipe": self.kd_serial_pipe,
                "target_info": self.kd_target_info,
                "last_event": self.kd_last_event,
                "log_path": self.kd_log_path,
            },
            "current_snapshot": self.current_snapshot,
            "term_sessions": [t.to_dict() for t in self.terms.values()],
            "guest_frozen": self.guest_frozen,
            "last_event": self.last_event,
        }


class StateStore:
    """All VmRuntime objects plus a session-relative clock."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._vms: dict[str, VmRuntime] = {}

    def vm(self, name: str) -> VmRuntime:
        """Runtime for a VM, created on first use."""
        if name not in self._vms:
            self._vms[name] = VmRuntime(name=name)
        return self._vms[name]

    def all(self) -> list[VmRuntime]:
        """Every runtime seen so far."""
        return list(self._vms.values())

    def term(self, session_id: str) -> TermInfo | None:
        """Find a terminal session across VMs."""
        for runtime in self._vms.values():
            info = runtime.terms.get(session_id)
            if info is not None:
                return info
        return None

    def t_plus(self) -> str:
        """Session-relative timestamp like T+01:23.456."""
        elapsed = max(0.0, time.time() - self.started_at)
        minutes = int(elapsed // 60)
        seconds = elapsed - minutes * 60
        return f"T+{minutes:02d}:{seconds:06.3f}"

    def record_event(self, vm: str, kind: str, **data: Any) -> dict[str, Any]:
        """Store the last notable event for a VM and return it."""
        event: dict[str, Any] = {"kind": kind, "at": time.time(), "t_plus": self.t_plus()}
        event.update(data)
        self.vm(vm).last_event = event
        return event

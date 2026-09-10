"""sys_*: aggregate state and host health."""

from __future__ import annotations

import asyncio
import contextlib
import platform
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ntdrive.config import VmConfig
from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.state import PowerState
from ntdrive.core.tools.common import NoParams
from ntdrive.errors import NtDriveError

# The guest probe must stay quick. `vmrun getGuestIPAddress -wait` blocks for as long as VMware
# Tools report nothing, so it gets a short timeout here instead of the 60 s the terminal uses.
GUEST_IP_TIMEOUT = 5.0


class StateParams(BaseModel):
    """sys_state."""

    model_config = ConfigDict(extra="forbid")

    vm: str | None = Field(default=None, description="Limit to one VM")


@tool(
    "sys_state",
    "VM power, debugger state, terminal sessions and last events in one answer.",
    StateParams,
    positional=(),
)
async def sys_state(service: NtDriveService, p: StateParams) -> dict[str, Any]:
    """Aggregate state."""
    names = [p.vm] if p.vm else list(service.config.vms)
    vms: list[dict[str, Any]] = []
    for name in names:
        cfg = service.vm_cfg(name)
        with contextlib.suppress(NtDriveError):
            await service.refresh_power(cfg)
        vms.append(service.runtime(name).to_dict())
    return {
        "daemon": {
            "version": service.version,
            "started_at": service.state.started_at,
            "t_plus": service.state.t_plus(),
            "log_dir": str(service.log_dir),
        },
        "vms": vms,
    }


def _vmx_has_serial_pipe(vmx: str, pipe: str) -> bool:
    """True when the vmx already exposes serial0 as the expected host named pipe."""
    try:
        text = Path(vmx).read_text(encoding="latin-1")
    except OSError:
        return False
    m = re.search(r'(?im)^\s*serial0\.fileName\s*=\s*"(.*)"\s*$', text)
    return m is not None and m.group(1).lower() == pipe.lower()


def _udp_port_free(port: int) -> bool:
    """True when nothing on the host holds this UDP port. kd.exe listens on it for KDNET.

    Windows lets a wildcard bind coexist with a bind to one address, so the probe asks for
    exclusive use, which fails against a holder on any address.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _config_issues(service: NtDriveService, cfg: VmConfig) -> list[str]:
    """Static checks: things that are wrong in vms.yaml or the vmx before any VM is touched."""
    issues: list[str] = []
    if cfg.backend not in service.adapters:
        issues.append(f"backend {cfg.backend} unsupported")
    vmx_ok = bool(cfg.vmx) and Path(cfg.vmx).is_file()
    if cfg.vmx and not vmx_ok:
        issues.append("vmx path does not exist")
    if cfg.kd_transport == "net":
        if not cfg.kdnet_hostip:
            issues.append("kdnet_hostip missing")
        if not cfg.kdnet.key:
            issues.append("kdnet key not set (run kd_setup_guest)")
    elif vmx_ok and not _vmx_has_serial_pipe(cfg.vmx, cfg.resolved_serial_pipe()):
        issues.append("serial pipe not in the vmx (run kd_setup_host with the VM off)")
    if cfg.guest.password_env and not cfg.guest.resolve_password():
        issues.append(f"environment variable {cfg.guest.password_env} is empty")
    if cfg.encryption_password_env and not cfg.resolve_encryption_password():
        issues.append(f"environment variable {cfg.encryption_password_env} is empty")
    return issues


async def _probe_vm(service: NtDriveService, cfg: VmConfig, issues: list[str]) -> dict[str, Any]:
    """Live checks for one VM, each bounded to a few seconds.

    Reports the power state, the host side of the debugger transport (a serial pipe server, or
    a free KDNET port) and whether the guest SSH port answers. A guest frozen at a kd prompt is
    not probed: neither VMware Tools nor OpenSSH can answer while the target is stopped.
    """
    runtime = service.runtime(cfg.name)
    power = PowerState.UNKNOWN
    try:
        power = await service.refresh_power(cfg)
    except NtDriveError as exc:
        issues.append(f"power state unknown: {exc.message}")
    running = power == PowerState.RUNNING
    kd = service.kd_sessions.get(cfg.name)
    attached = kd is not None and kd.attached
    serial_pipe: dict[str, Any] | None = None
    kdnet_port: dict[str, Any] | None = None
    if cfg.kd_transport == "serial":
        pipe = cfg.resolved_serial_pipe()
        pipe_open = service.serial_pipe_open(pipe) if running else None
        serial_pipe = {"path": pipe, "open": pipe_open}
        if pipe_open is False:
            issues.append(
                f"serial pipe {pipe} has no server on the host (the running VM does not expose "
                "serial0, or another debugger took the pipe)"
            )
    else:
        free = _udp_port_free(cfg.kdnet.port)
        kdnet_port = {"port": cfg.kdnet.port, "free": free, "held_by_ntdrive": attached}
        if not free and not attached:
            issues.append(
                f"UDP port {cfg.kdnet.port} is taken by another process, so kd.exe cannot "
                "listen on it (change kdnet.port or stop the other debugger)"
            )
    guest: dict[str, Any] = {
        "ip": None,
        "ssh_port": cfg.guest.ssh_port,
        "ssh_open": None,
        "skipped": None,
    }
    if not running:
        guest["skipped"] = "vm_not_running"
    elif runtime.guest_frozen:
        guest["skipped"] = "guest_frozen_by_debugger"
    else:
        try:
            ssh_open, ip = await service.ssh_reachable(cfg, timeout=GUEST_IP_TIMEOUT)
        except NtDriveError as exc:
            issues.append(f"guest IP unknown, VMware Tools may not be running: {exc.message}")
        else:
            guest["ip"], guest["ssh_open"] = ip, ssh_open
            if not ssh_open:
                issues.append(
                    f"SSH port {cfg.guest.ssh_port} on {ip} does not answer (install and start "
                    "OpenSSH in the guest, or check the guest firewall)"
                )
    return {
        "power": str(power),
        "kd_state": str(runtime.kd_state),
        "guest": guest,
        "serial_pipe": serial_pipe,
        "kdnet_port": kdnet_port,
    }


async def _vm_health(service: NtDriveService, name: str, cfg: VmConfig) -> dict[str, Any]:
    issues = _config_issues(service, cfg)
    live = await _probe_vm(service, cfg, issues)
    return {
        "name": name,
        "backend": cfg.backend,
        "kd_transport": cfg.kd_transport,
        **live,
        "issues": issues,
    }


@tool(
    "sys_health",
    "Check binaries, config and backend capabilities, then probe every VM: power, guest SSH "
    "port and the debugger transport on the host. Run this first.",
    NoParams,
    positional=(),
)
async def sys_health(service: NtDriveService, _: NoParams) -> dict[str, Any]:
    """Host health plus a live probe of each VM."""
    host = service.config.host
    backends: dict[str, Any] = {}
    for name, adapter in service.adapters.items():
        try:
            info = await adapter.health()
        except NtDriveError as exc:
            info = {"backend": name, "error": exc.to_dict()["error"]}
        info["capabilities"] = adapter.capabilities()
        backends[name] = info
    kd_ok = Path(host.kd).is_file()
    kdnet_ok = Path(host.kdnet).is_file()
    problems: list[str] = []
    if not service.config.path:
        problems.append("vms.yaml not found; copy vms.example.yaml to vms.yaml")
    if not kd_ok:
        problems.append(f"kd.exe not found at {host.kd}")
    if not kdnet_ok:
        problems.append(f"kdnet.exe not found at {host.kdnet}")
    for name, info in backends.items():
        if not info.get("exists", False):
            problems.append(f"{name} binary not found at {info.get('path')}")
    vms = list(
        await asyncio.gather(
            *(_vm_health(service, name, cfg) for name, cfg in service.config.vms.items())
        )
    )
    return {
        "ok": not problems,
        "problems": problems,
        "version": service.version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "config_path": service.config.path,
        "log_dir": str(service.log_dir),
        "binaries": {
            "kd": {"path": host.kd, "exists": kd_ok},
            "kdnet": {"path": host.kdnet, "exists": kdnet_ok},
        },
        "backends": backends,
        "daemon_bind": host.daemon_bind,
        "tools": len(service.registry),
        "vms": vms,
        "checked_at": time.time(),
    }

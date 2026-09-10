"""sys_*: aggregate state and host health."""

from __future__ import annotations

import contextlib
import platform
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.tools.common import NoParams
from ntdrive.errors import NtDriveError


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


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


@tool(
    "sys_health",
    "Check binaries, config, backend capabilities and ports. Run this first.",
    NoParams,
    positional=(),
)
async def sys_health(service: NtDriveService, _: NoParams) -> dict[str, Any]:
    """Host health."""
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
    vms: list[dict[str, Any]] = []
    for name, cfg in service.config.vms.items():
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
        vms.append(
            {
                "name": name,
                "backend": cfg.backend,
                "kd_transport": cfg.kd_transport,
                "issues": issues,
            }
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
        "daemon_port_free_now": _port_free(host.bind_host, host.bind_port),
        "tools": len(service.registry),
        "vms": vms,
        "checked_at": time.time(),
    }

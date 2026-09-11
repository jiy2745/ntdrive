"""kd_*: kernel debugging through kd.exe over KDNET or a VMware serial pipe."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from pydantic import Field

from ntdrive.config import save_kdnet_settings
from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import BACKEND_ERROR, INVALID_ARGS, NtDriveError
from ntdrive.kd.firewall import MANUAL_FIREWALL_HINT
from ntdrive.kd.session import generate_kdnet_key

# A KDNET key is four base36 words joined by dots. Anything else must never reach the bcdedit
# command line that runs inside the guest shell.
KDNET_KEY = r"^[0-9a-z]{1,13}(\.[0-9a-z]{1,13}){3}$"


def _check_hostip(value: str) -> str:
    """Strict IPv4 so vms.yaml cannot smuggle shell text into the guest command."""
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise NtDriveError(
            INVALID_ARGS, f"kdnet_hostip is not an IPv4 address: {value!r}", "fix vms.yaml"
        ) from None
    return value


def _check_key(value: str) -> str:
    if not re.match(KDNET_KEY, value):
        raise NtDriveError(
            INVALID_ARGS,
            "kdnet key must be four base36 words separated by dots",
            "omit key to have one generated",
        )
    return value


class SetupParams(VmParams):
    """kd_setup_guest."""

    port: int | None = Field(default=None, ge=49152, le=65535, description="KDNET UDP port")
    key: str | None = Field(
        default=None, pattern=KDNET_KEY, description="KDNET key; generated when omitted"
    )


class AttachParams(VmParams):
    """kd_attach."""

    port: int | None = Field(default=None, ge=1, le=65535, description="Override the port")
    key: str | None = Field(default=None, pattern=KDNET_KEY, description="Override the key")
    symbol_path: str | None = Field(default=None, description="Override host.symbol_path")
    wait_for_target: bool = Field(default=True, description="Block until the target connects")
    timeout: float = Field(default=120, ge=1, description="Seconds to wait for the target")


class DetachParams(VmParams):
    """kd_detach."""

    force: bool = Field(default=False, description="Kill kd.exe without resuming the target")


class BreakParams(VmParams):
    """kd_break."""

    timeout: float = Field(default=10, ge=1, description="Seconds to wait for the prompt")


class ExecParams(VmParams):
    """kd_exec."""

    cmd: str | None = Field(default=None, description="One debugger command")
    cmds: list[str] | None = Field(default=None, description="Several commands, run in order")
    timeout: float = Field(default=60, ge=1, description="Seconds per command")
    max_bytes: int = Field(default=65536, ge=256, description="Cap on each command's output")


class WaitParams(VmParams):
    """kd_wait_event."""

    timeout: float = Field(default=300, ge=1, description="Seconds to wait for a break")


class LogTailParams(VmParams):
    """kd_log_tail."""

    bytes: int = Field(default=16384, ge=1, description="How many bytes from the end")


class SetupHostParams(VmParams):
    """kd_setup_host."""

    fix_firewall: bool = Field(
        default=True,
        description="net: when the host firewall blocks kd.exe, repair it through one UAC prompt",
    )
    timeout: float = Field(default=120, ge=1, description="net: seconds to wait for the UAC prompt")


@tool(
    "kd_setup_host",
    "Prepare the host side of the kd transport: serial adds the named-pipe COM port to the vmx "
    "(VM must be off), net checks the host firewall for kd.exe and repairs it through one UAC "
    "prompt.",
    SetupHostParams,
)
async def kd_setup_host(service: NtDriveService, p: SetupHostParams) -> dict[str, Any]:
    """Host-side transport setup. Nothing here touches the guest."""
    cfg = service.vm_cfg(p.vm)
    if cfg.kd_transport == "serial":
        pipe = cfg.resolved_serial_pipe()
        changed = await service.adapter_for(cfg).ensure_serial_pipe(cfg, pipe)
        service.state.record_event(p.vm, "kd_setup_host", transport="serial", changed=changed)
        return {
            "vm": p.vm,
            "transport": "serial",
            "serial_pipe": pipe,
            "changed": changed,
            "next": "vm_start, then kd_setup_guest (bcdedit serial), then reboot the guest",
        }
    # net: kd.exe must be allowed to receive UDP. Reading the rules is free, changing them
    # takes one UAC prompt that a person at the desktop has to approve.
    status = await service.kdnet_firewall()
    changed = False
    if not status.ok and p.fix_firewall:
        if not status.checked:
            raise NtDriveError(BACKEND_ERROR, status.problem(), MANUAL_FIREWALL_HINT)
        status = await service.fix_kdnet_firewall(p.timeout)
        changed = True
        if not status.ok:
            raise NtDriveError(
                BACKEND_ERROR,
                "the host firewall still blocks KDNET after the repair: " + status.problem(),
                MANUAL_FIREWALL_HINT,
            )
    service.state.record_event(p.vm, "kd_setup_host", transport="net", changed=changed)
    if status.ok:
        next_step = "kd_setup_guest (bcdedit net), then reboot the guest, then kd_attach"
    elif not status.checked:
        # A repair needs a readable rule set first, so only the manual route applies.
        next_step = MANUAL_FIREWALL_HINT
    else:
        next_step = (
            "kd_setup_host with fix_firewall=true repairs the firewall through one UAC prompt, "
            "or " + MANUAL_FIREWALL_HINT
        )
    return {
        "vm": p.vm,
        "transport": "net",
        "changed": changed,
        "kdnet_hostip": cfg.kdnet_hostip,
        "firewall": status.as_dict(),
        "next": next_step,
    }


@tool(
    "kd_setup_guest",
    "Enable kernel debugging in the guest with bcdedit over SSH (serial or KDNET per "
    "kd_transport) and store the KDNET port and key in vms.yaml.",
    SetupParams,
)
async def kd_setup_guest(service: NtDriveService, p: SetupParams) -> dict[str, Any]:
    """Configure the target for kernel debugging. Needs SSH to the guest, then a reboot.

    Uses the VM's kd_transport: `serial` writes a serial debug setting (no host firewall or admin
    needed) and `net` writes KDNET with a host IP, port and key.
    """
    cfg = service.vm_cfg(p.vm)
    serial = cfg.kd_transport == "serial"
    if not serial and not cfg.kdnet_hostip:
        raise NtDriveError(
            INVALID_ARGS,
            f"vms.yaml has no kdnet_hostip for {p.vm}",
            "set it to the IPv4 of the host's VMnet8 adapter, or use kd_transport: serial",
        )
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    port = p.port or cfg.kdnet.port
    key = "" if serial else _check_key(p.key or cfg.kdnet.key or generate_kdnet_key())
    if not serial:
        _check_hostip(cfg.kdnet_hostip)
    transport = await service.transport(cfg)
    if not hasattr(transport, "exec_once"):
        raise NtDriveError(BACKEND_ERROR, "the transport cannot run commands")
    if serial:
        commands = [
            "bcdedit /debug on",
            "bcdedit /dbgsettings serial debugport:1 baudrate:115200",
            "bcdedit /dbgsettings",
        ]
    else:
        commands = [
            "bcdedit /debug on",
            f"bcdedit /dbgsettings net hostip:{cfg.kdnet_hostip} port:{port} key:{key}",
            "bcdedit /dbgsettings",
        ]

    def redact(text: str) -> str:
        # The KDNET key is a secret. Keep it out of returns, hints and the audit log.
        return text.replace(key, "***") if key else text

    outputs: list[dict[str, Any]] = []
    for command in commands:
        code, out = await transport.exec_once(command, timeout=60)
        outputs.append(
            {"cmd": redact(command), "exit_code": code, "output": redact(out.strip()[-2000:])}
        )
        if code != 0:
            raise NtDriveError(
                BACKEND_ERROR,
                f"'{redact(command)}' failed in the guest: {redact(out.strip()[:300])}",
                "the SSH user needs administrator rights and Secure Boot must be off",
                outputs=outputs,
            )
    if not serial:
        save_kdnet_settings(service.config, p.vm, port, key)
        session = service.kd_sessions.get(p.vm)
        if session is not None:
            session.port, session.key = port, key
    service.state.record_event(p.vm, "kd_setup_guest", transport=cfg.kd_transport)
    return {
        "vm": p.vm,
        "transport": cfg.kd_transport,
        "port": None if serial else port,
        "key_saved": not serial,
        "needs_reboot": True,
        "steps": outputs,
    }


@tool(
    "kd_attach",
    "Start kd.exe for the VM and (by default) wait until the target connects.",
    AttachParams,
    long_poll=True,
)
async def kd_attach(service: NtDriveService, p: AttachParams) -> dict[str, Any]:
    """Attach the debugger."""
    cfg = service.vm_cfg(p.vm)
    key = p.key or cfg.kdnet.key
    if cfg.kd_transport == "net" and not key:
        raise NtDriveError(
            INVALID_ARGS,
            f"no KDNET key for {p.vm}",
            "run kd_setup_guest first or put kdnet.key into vms.yaml",
        )
    session = service.kd_session(cfg, port=p.port, key=key)
    if p.symbol_path:
        session.symbol_path = p.symbol_path
    status = await session.attach(wait_for_target=p.wait_for_target, timeout=p.timeout)
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_attach", state=status["state"])
    return {"vm": p.vm, **status}


@tool("kd_detach", "Resume the target if needed and stop kd.exe.", DetachParams)
async def kd_detach(service: NtDriveService, p: DetachParams) -> dict[str, Any]:
    """Detach."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    status = await session.detach(force=p.force)
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_detach")
    return {"vm": p.vm, **status}


@tool("kd_break", "Break into the running target and wait for the kd> prompt.", BreakParams)
async def kd_break(service: NtDriveService, p: BreakParams) -> dict[str, Any]:
    """Break in. The guest is frozen from here until kd_go."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    result = await session.break_in(timeout=p.timeout)
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_break")
    return {"vm": p.vm, **result}


@tool("kd_go", "Resume the target (g).", VmParams)
async def kd_go(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Resume."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    status = await session.go()
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_go")
    return {"vm": p.vm, **status}


@tool(
    "kd_exec",
    "Run one or more debugger commands at the kd> prompt and return each command's output.",
    ExecParams,
    positional=("vm", "cmd"),
)
async def kd_exec(service: NtDriveService, p: ExecParams) -> dict[str, Any]:
    """Execute commands; needs a broken-in target."""
    cmds = list(p.cmds or [])
    if p.cmd:
        cmds.insert(0, p.cmd)
    if not cmds:
        raise NtDriveError(INVALID_ARGS, "kd_exec needs cmd or cmds")
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    outputs = await session.exec(cmds, timeout=p.timeout, max_bytes=p.max_bytes)
    return {"vm": p.vm, "outputs": outputs, "state": str(session.state)}


@tool(
    "kd_wait_event",
    "Wait until the running target stops (bugcheck, breakpoint, ...) or the timeout expires.",
    WaitParams,
    long_poll=True,
)
async def kd_wait_event(service: NtDriveService, p: WaitParams) -> dict[str, Any]:
    """Long-poll for a break."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    event = await session.wait_event(timeout=p.timeout)
    service.runtime(p.vm)
    if event.get("event") not in ("timeout", None):
        service.state.record_event(p.vm, "kd_event", event=event.get("event"))
    return {"vm": p.vm, **event}


@tool("kd_state", "Debugger state, transport, target info, last event and log path.", VmParams)
async def kd_state(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Status."""
    service.vm_cfg(p.vm)
    session = service.kd_sessions.get(p.vm)
    if session is None:
        runtime = service.runtime(p.vm)
        return {
            "vm": p.vm,
            "state": "detached",
            "transport": runtime.kd_transport,
            "port": runtime.kd_port,
            "serial_pipe": runtime.kd_serial_pipe,
            "target_info": "",
            "last_event": None,
            "log_path": "",
            "pid": None,
        }
    return {"vm": p.vm, **session.status()}


@tool("kd_log_tail", "Last bytes of the kd.exe transcript.", LogTailParams)
async def kd_log_tail(service: NtDriveService, p: LogTailParams) -> dict[str, Any]:
    """Transcript tail."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    return {"vm": p.vm, "text": session.log_tail(p.bytes), "log_path": str(session.log_path)}

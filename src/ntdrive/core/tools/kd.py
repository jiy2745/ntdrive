"""kd_*: kernel debugging through kd.exe over KDNET or a VMware serial pipe."""

from __future__ import annotations

import contextlib
import ipaddress
import re
from typing import Any

from pydantic import Field

from ntdrive.config import VmConfig, save_kdnet_settings
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


def _parse_bcd(text: str) -> dict[str, str]:
    """Bcdedit output as {name: value} with lower-cased names. Localized trailers are skipped."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z]+)\s{2,}(\S.*?)\s*$", line)
        if m:
            values[m.group(1).lower()] = m.group(2)
    return values


async def _configure_guest(
    service: NtDriveService, cfg: VmConfig, port_override: int | None, key_override: str | None
) -> dict[str, Any]:
    """The work of kd_setup_guest. kd_attach runs it too when no KDNET key is saved yet."""
    serial = cfg.kd_transport == "serial"
    if not serial and not cfg.kdnet_hostip:
        raise NtDriveError(
            INVALID_ARGS,
            f"vms.yaml has no kdnet_hostip for {cfg.name}",
            "set it to the IPv4 of the host's VMnet8 adapter, or use kd_transport: serial",
        )
    if not serial:
        _check_hostip(cfg.kdnet_hostip)
    service.ensure_not_frozen(cfg.name)
    await service.ensure_running(cfg)
    transport = await service.transport(cfg)
    if not hasattr(transport, "exec_once"):
        raise NtDriveError(BACKEND_ERROR, "the transport cannot run commands")

    # The KDNET key is a secret: it stays out of returns, hints and the audit log. The list grows
    # as keys become known (the guest's, then the one written).
    secrets: list[str] = []

    def redact(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, "***")
        return text

    outputs: list[dict[str, Any]] = []

    async def run(command: str) -> str:
        code, out = await transport.exec_once(command, timeout=60)
        outputs.append({"cmd": redact(command), "exit_code": code, "output": out.strip()[-2000:]})
        if code != 0:
            raise NtDriveError(
                BACKEND_ERROR,
                f"'{redact(command)}' failed in the guest: {redact(out.strip()[:300])}",
                "the SSH user needs administrator rights and Secure Boot must be off",
                outputs=[{**entry, "output": redact(entry["output"])} for entry in outputs],
            )
        return str(out)

    # Ports the other net VMs of this host use: kd.exe listens on the host, one port per target.
    taken = {
        other.kdnet.port
        for name, other in service.config.vms.items()
        if name != cfg.name and other.kd_transport == "net"
    }
    adopted = False
    debug_on = False
    guest_key = ""
    guest_port: int | None = None
    note = ""
    if not serial:
        settings = _parse_bcd(await run("bcdedit /dbgsettings"))
        # The guest shell is PowerShell (setup-guest.ps1 sets DefaultShell). PowerShell parses a
        # bare {current} as a script block and, worse, turns it into -encodedCommand for the native
        # exe, so bcdedit sees /encodedCommand and fails. Single quotes keep it a literal.
        current = _parse_bcd(await run("bcdedit /enum '{current}'"))
        debug_on = current.get("debug", "").lower() in {"yes", "true"}
        guest_key = settings.get("key", "")
        if not re.match(KDNET_KEY, guest_key):
            guest_key = ""
        if guest_key:
            secrets.append(guest_key)
        with contextlib.suppress(ValueError):
            guest_port = int(settings.get("port", ""))
        to_this_host = (
            settings.get("debugtype", "").upper() == "NET"
            and settings.get("hostip", "") == cfg.kdnet_hostip
            and bool(guest_key)
            and guest_port is not None
        )
        adopted = (
            to_this_host
            and guest_port not in taken
            and key_override is None
            and port_override is None
        )
        if to_this_host and guest_port in taken and port_override is None:
            note = (
                f"the guest used port {guest_port}, which another VM of this host already has, "
                "so a free port was written instead"
            )
    if serial:
        port, key = cfg.kdnet.port, ""
    elif adopted and guest_port is not None:
        port, key = guest_port, guest_key
    else:
        port = port_override or cfg.kdnet.port
        while port_override is None and port in taken:
            port += 1
        key = _check_key(key_override or cfg.kdnet.key or guest_key or generate_kdnet_key())
        secrets.append(key)

    if serial:
        commands = [
            "bcdedit /debug on",
            "bcdedit /dbgsettings serial debugport:1 baudrate:115200",
            "bcdedit /dbgsettings",
        ]
    elif adopted:
        commands = [] if debug_on else ["bcdedit /debug on"]
    else:
        commands = [
            "bcdedit /debug on",
            f"bcdedit /dbgsettings net hostip:{cfg.kdnet_hostip} port:{port} key:{key}",
            "bcdedit /dbgsettings",
        ]
    for command in commands:
        await run(command)
    for entry in outputs:
        entry["output"] = redact(entry["output"])

    if not serial:
        save_kdnet_settings(service.config, cfg.name, port, key)
        session = service.kd_sessions.get(cfg.name)
        if session is not None:
            session.port, session.key = port, key
    service.state.record_event(
        cfg.name, "kd_setup_guest", transport=cfg.kd_transport, adopted=adopted
    )
    return {
        "vm": cfg.name,
        "transport": cfg.kd_transport,
        "port": None if serial else port,
        "key_saved": not serial,
        "adopted": adopted,
        "needs_reboot": serial or not adopted or not debug_on,
        "note": note or None,
        "steps": outputs,
    }


@tool(
    "kd_setup_guest",
    "Enable kernel debugging in the guest with bcdedit over SSH (serial or KDNET per "
    "kd_transport) and store the KDNET port and key in vms.yaml. A guest that already debugs to "
    "this host (scripts/setup-guest.ps1 sets that up) is read back instead of rewritten.",
    SetupParams,
)
async def kd_setup_guest(service: NtDriveService, p: SetupParams) -> dict[str, Any]:
    """Configure the target for kernel debugging over SSH.

    `serial` writes the serial debug setting and needs a reboot. `net` first reads what the guest
    has: when it already debugs to this host's IP (the guest script sets that up), the port and
    the key are read back and saved (`adopted`), and no reboot is needed if debugging is already
    on. Otherwise the KDNET settings are written with the host IP, a port and a key. kd_attach
    runs the same step itself when no key is saved yet.
    """
    return await _configure_guest(service, service.vm_cfg(p.vm), p.port, p.key)


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
        # A guest that ran scripts/setup-guest.ps1 already debugs to this host with a key of its
        # own. Read it back over SSH, the kd_setup_guest step, instead of failing.
        configured = await _configure_guest(service, cfg, None, None)
        if configured["needs_reboot"]:
            raise NtDriveError(
                BACKEND_ERROR,
                f"KDNET was just configured in the guest {p.vm} and needs a reboot",
                "vm_reboot mode=soft confirm=true, then kd_attach again",
                setup=configured,
            )
        key = cfg.kdnet.key
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

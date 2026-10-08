"""kd_*: kernel debugging through kd.exe over KDNET or a VMware serial pipe."""

from __future__ import annotations

import contextlib
import ipaddress
import re
import time
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ntdrive.config import VmConfig, save_kdnet_settings
from ntdrive.core.registry import tool
from ntdrive.core.state import KdState
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import (
    BACKEND_ERROR,
    INVALID_ARGS,
    KD_NOT_ATTACHED,
    KD_NOT_BROKEN,
    NtDriveError,
)
from ntdrive.kd.firewall import MANUAL_FIREWALL_HINT
from ntdrive.kd.session import generate_kdnet_key, parse_bugcheck

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

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
    wait_for_target: bool = Field(
        default=True,
        description=(
            "Block until the target connects (up to timeout). false returns at once with state "
            "waiting. A target that booted before kd listened needs vm_reboot to connect"
        ),
    )
    timeout: float = Field(default=120, ge=1, description="Seconds to wait for the target")


class DetachParams(VmParams):
    """kd_detach."""

    force: bool = Field(default=False, description="Kill kd.exe without resuming the target")


class BreakParams(VmParams):
    """kd_break."""

    timeout: float = Field(default=20, ge=1, description="Seconds to wait for the prompt")


class ExecParams(VmParams):
    """kd_exec."""

    cmd: str | None = Field(default=None, description="One debugger command")
    cmds: list[str] | None = Field(default=None, description="Several commands, run in order")
    timeout: float = Field(default=60, ge=1, description="Seconds per command")
    max_bytes: int = Field(
        default=16384,
        ge=256,
        le=1 << 20,
        description=(
            "Cap on each command's output. Raise it when truncated says so. The default is "
            "deliberately small because commands like !analyze -v print tens of kilobytes of "
            "chkimg noise"
        ),
    )
    processor: int | None = Field(
        default=None,
        ge=0,
        le=1023,
        description=(
            "Switch to this processor (~Ns) before the commands. A break lands on whichever "
            "processor hit it, and kd_state's last_event reports which one, so pass it to run "
            "where the break happened. The switch resolves symbols, so over a network symbol path "
            "it can wedge on the first lookup: set .sympath cache* or pre-.reload /f first"
        ),
    )


class CaptureFaultParams(VmParams):
    """kd_capture_fault."""

    processor: int | None = Field(
        default=None,
        ge=0,
        le=1023,
        description=(
            "Processor to switch to (~Ns). Defaults to the one the bugcheck broke on, from "
            "kd_state last_event"
        ),
    )
    fault_rip: str | None = Field(
        default=None,
        description=(
            "Faulting instruction address to find on the stack. Defaults to bugcheck Arg3 (the "
            "0x50 PAGE_FAULT case). Pass it for bugchecks that carry the fault address elsewhere"
        ),
    )
    context_record: str | None = Field(
        default=None,
        description=(
            "A CONTEXT pointer to .cxr instead of searching the stack, for bugchecks that pass one "
            "(0x3B / 0x7E Arg3). Takes precedence over fault_rip"
        ),
    )
    trap_offset: int = Field(
        default=0x168,
        ge=0,
        le=0x1000,
        description=(
            "Offset of Rip inside KTRAP_FRAME on x64 (0x168 on current Windows). The trap frame "
            "base is a stack match of fault_rip minus this"
        ),
    )
    match_index: int = Field(
        default=0,
        ge=0,
        description=(
            "Which stack match of fault_rip is the KTRAP_FRAME, when several carry the same value. "
            "All candidates are returned so another can be chosen"
        ),
    )
    search_quads: int = Field(
        default=0x800,
        ge=0x10,
        le=0x4000,
        description="How many quadwords above rsp to scan for the trap frame",
    )
    cache_symbols: bool = Field(
        default=True,
        description=(
            "Set a cache-only symbol path (.sympath cache*<cache>) for the session first, so the "
            "symbol-heavy capture cannot wedge on a network symbol server and drop the bugcheck. "
            "false keeps the current path"
        ),
    )
    timeout: float = Field(default=30, ge=1, description="Seconds per debugger command")


class SampleParams(VmParams):
    """kd_sample."""

    symbol: str = Field(description="Symbol or address to break on, for example mod!Class::Method")
    exprs: list[str] = Field(
        default_factory=list,
        description=(
            "Debugger commands to run at each hit, for example ['poi(@rcx)', 'du poi(@rdx)']"
        ),
    )
    n: int = Field(default=8, ge=1, le=200, description="How many hits to collect before stopping")
    condition: str | None = Field(
        default=None,
        description=(
            "Expression evaluated by the daemon at each hit (with `?`). A hit whose value is zero "
            "is skipped and not returned. It is never compiled into the breakpoint"
        ),
    )
    max_seconds: float = Field(
        default=120, ge=1, description="Wall-clock cap, so a hot symbol cannot run forever"
    )
    timeout: float = Field(default=60, ge=1, description="Seconds to wait for each hit")


class WaitParams(VmParams):
    """kd_wait_event."""

    timeout: float = Field(default=300, ge=1, description="Seconds to wait for a break")


class LogTailParams(VmParams):
    """kd_log_tail."""

    bytes: int = Field(default=16384, ge=1, le=1 << 20, description="How many bytes from the end")


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
    effect="additive",
    idempotent=True,
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
        # The guest shell is PowerShell (setup-guest.cmd sets DefaultShell). PowerShell parses a
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
    "kd_transport) and save the KDNET port and key to vms.yaml. Settings that already point "
    "at this host are read back, not rewritten.",
    SetupParams,
    effect="additive",
    idempotent=True,
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
    "Start kd.exe for the VM and wait until the target connects.",
    AttachParams,
    long_poll=True,
    effect="additive",
)
async def kd_attach(service: NtDriveService, p: AttachParams) -> dict[str, Any]:
    """Attach the debugger."""
    cfg = service.vm_cfg(p.vm)
    key = p.key or cfg.kdnet.key
    if cfg.kd_transport == "net" and not key:
        # A guest that ran scripts/setup-guest.cmd already debugs to this host with a key of its
        # own. Read it back over SSH, the kd_setup_guest step, instead of failing.
        configured = await _configure_guest(service, cfg, None, None)
        if configured["needs_reboot"]:
            raise NtDriveError(
                BACKEND_ERROR,
                f"KDNET was just configured in the guest {p.vm} and needs a reboot",
                "vm_reboot mode=soft, then kd_attach again",
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


@tool("kd_detach", "Resume the target if needed and stop kd.exe.", DetachParams, effect="additive")
async def kd_detach(service: NtDriveService, p: DetachParams) -> dict[str, Any]:
    """Detach."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    was_connected = session.transport == "net" and bool(session.target_info)
    status = await session.detach(force=p.force)
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_detach")
    result = {"vm": p.vm, **status}
    if was_connected:
        # Quitting kd.exe while a KDNET target is live makes kd print transport chatter, sometimes
        # "A fatal system error has occurred". That is kd.exe reacting to the dropped link, not a
        # guest crash: the guest keeps running. Say so, since it has scared callers.
        result["note"] = (
            "detaching from a live KDNET target can print a transport error such as 'A fatal "
            "system error has occurred'. That is kd.exe, not the guest: the guest keeps running"
        )
    return result


@tool(
    "kd_break",
    "Break into the running target and wait for the kd> prompt.",
    BreakParams,
    effect="additive",
)
async def kd_break(service: NtDriveService, p: BreakParams) -> dict[str, Any]:
    """Break in. The guest is frozen from here until kd_go."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    result = await session.break_in(timeout=p.timeout)
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_break")
    return {"vm": p.vm, **result}


@tool("kd_go", "Resume the target (g).", VmParams, effect="additive")
async def kd_go(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Resume."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    status = await session.go()
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_go")
    return {"vm": p.vm, **status}


# A breakpoint that resumes the target itself: `bp addr "j (c) '.printf ..;gc'; 'gc'"`.
_BP_SET_RE = re.compile(r"^\s*b[pmua]\d*\s", re.IGNORECASE)
_AUTO_RESUME_RE = re.compile(r"\bgc\b|'\s*g\s*'|\"\s*g\s*\"", re.IGNORECASE)


def _auto_resume_bps(cmds: list[str]) -> list[str]:
    """Breakpoint commands that resume the target themselves, the KDNET round-trip flood."""
    return [c for c in cmds if _BP_SET_RE.match(c) and _AUTO_RESUME_RE.search(c)]


@tool(
    "kd_exec",
    "Run one or more debugger commands at the kd> prompt and return each command's output. A "
    "running target (right after kd_attach, or after kd_go) is broken into first, so the first "
    "command no longer fails with kd_not_broken. A symbol-heavy command (x, u, ln, lm, a first "
    "!extension) or a processor switch can be slow while symbols download: over a network symbol "
    "path the first lookup blocks on the server, so set a cache-only path "
    "(.sympath cache*C:\\symbols) or pre-fetch (.reload /f) once symbols are cached. Raise the "
    "timeout, and if one wedges ntdrive interrupts it so the next kd_exec works (at a bugcheck the "
    "interrupt keeps the break, but do not kd_detach there: detaching resumes and reboots). A "
    "breakpoint that resumes itself (gc) on a hot path NMIs the guest over KDNET: use kd_sample.",
    ExecParams,
    positional=("vm", "cmd"),
    effect="destructive",
)
async def kd_exec(service: NtDriveService, p: ExecParams) -> dict[str, Any]:
    """Execute commands, breaking the target in first if it is running."""
    cmds = list(p.cmds or [])
    if p.cmd:
        cmds.insert(0, p.cmd)
    if not cmds:
        raise NtDriveError(INVALID_ARGS, "kd_exec needs cmd or cmds")
    if p.processor is not None:
        # Run where the caller asked. kd resets the implicit process and processor at every break,
        # so anything that needs a context (`.process /r /p` for session space) must set it in the
        # same call as the commands that use it.
        cmds.insert(0, f"~{p.processor}s")
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    broke_in = False
    if session.attached and session.state != KdState.BROKEN:
        # A command needs a kd> prompt, and kd_attach and kd_go both leave the target running, so
        # the first kd_exec used to fail with kd_not_broken and need a manual kd_break. Break in
        # here: running a command is why kd_exec was called. The target is left broken, as after a
        # manual kd_break, and kd_go resumes it.
        await session.break_in(timeout=min(p.timeout, 20.0))
        service.runtime(p.vm)  # a break freezes the guest; keep guest_frozen in step
        broke_in = True
    outputs = await session.exec(cmds, timeout=p.timeout, max_bytes=p.max_bytes)
    result: dict[str, Any] = {"vm": p.vm, "outputs": outputs, "state": str(session.state)}
    if broke_in:
        result["note"] = "the target was running, so kd_exec broke in first. kd_go resumes it"
    risky = _auto_resume_bps(cmds)
    if risky:
        # The breakpoint is already set by the time this returns, so the warning rides the result
        # the caller reads before it calls kd_go and lets the thing fire.
        result["warning"] = (
            "a breakpoint that resumes the target itself (gc) costs one KDNET round trip per hit. "
            "On a hot path that floods the link and NMIs the guest with bugcheck 0x80, which has "
            f"already cost live sessions their snapshots: {risky}. kd_sample does the same job "
            "safely, with a plain breakpoint and the condition evaluated on the host. Clear this "
            "one with bc before kd_go if the address is hit often"
        )
    return result


@tool(
    "kd_bugcheck",
    "Classify the current bugcheck cheaply: runs .bugcheck (two lines) and returns the code, its "
    "arguments and the faulting instruction. Use this before !analyze -v, which takes tens of "
    "seconds and prints tens of kilobytes of chkimg noise that is false on a patched kernel.",
    VmParams,
    effect="read",
)
async def kd_bugcheck(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """The bugcheck code and arguments, without paying for !analyze -v."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    outputs = await session.exec([".bugcheck", "r rip", "u rip L1"], timeout=30, max_bytes=4096)
    by_cmd = {str(o["cmd"]): str(o["output"]) for o in outputs}
    parsed = parse_bugcheck(by_cmd.get(".bugcheck", ""))
    result: dict[str, Any] = {
        "vm": p.vm,
        "bugcheck": parsed,
        "raw": by_cmd.get(".bugcheck", "").strip(),
        "rip": by_cmd.get("r rip", "").strip(),
        "faulting_instruction": by_cmd.get("u rip L1", "").strip(),
        "state": str(session.state),
    }
    if parsed is None:
        result["note"] = (
            ".bugcheck reported no code, so the target is probably not in a bugcheck. "
            "kd_wait_event returns the code and arguments directly when it catches one"
        )
    return result


def _local_symbol_cache(sympath: str) -> str:
    r"""The local cache directory out of a symbol path, for a cache-only .sympath.

    `srv*C:\symbols*https://...` caches downloads under `C:\symbols`, so a cache-only path that
    serves those without touching the network is `cache*C:\symbols`. The cache is the first element
    that looks like a drive path; with none, the ntdrive default is assumed.
    """
    for part in sympath.split("*"):
        if re.match(r"^[A-Za-z]:[\\/]", part):
            return part
    return r"C:\symbols"


def _stack_search_hits(text: str) -> list[int]:
    """Addresses from `s -q` output: each match line leads with the address of the found value."""
    hits: list[int] = []
    for line in text.splitlines():
        m = re.match(r"\s*([0-9a-fA-F`]{8,19})\b", line)
        if not m:
            continue
        token = m.group(1).replace("`", "")
        if re.fullmatch(r"[0-9a-fA-F]{8,16}", token):
            with contextlib.suppress(ValueError):
                hits.append(int(token, 16))
    return hits


@tool(
    "kd_capture_fault",
    "Capture an OOB-write or fault bugcheck in one call: switch to the processor the bugcheck "
    "broke on, find the KTRAP_FRAME on the stack by the faulting RIP (bugcheck Arg3 by default) "
    "and .trap it (or .cxr a context_record), then return the unwound call stack (kb), the "
    "faulting instruction and the kernel module bases (lm). Needs a bugcheck break (kd_wait_event "
    "catches one). It sets a cache-only symbol path first so the symbol-heavy commands cannot "
    "wedge on a network symbol server and drop the target mid-capture (cache_symbols=false keeps "
    "the current path). Pass fault_rip or context_record for bugchecks that do not carry the fault "
    "address in Arg3. Do not kd_detach or kd_go afterwards to leave: both resume and complete the "
    "crash, losing the bugcheck.",
    CaptureFaultParams,
    long_poll=True,
    effect="destructive",
)
async def kd_capture_fault(service: NtDriveService, p: CaptureFaultParams) -> dict[str, Any]:
    """One-shot fault capture for a bugcheck: the manual .trap/kb/lm round trip, automated."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    if not session.attached:
        raise NtDriveError(KD_NOT_ATTACHED, f"kd is not attached to {p.vm}", "call kd_attach first")
    if session.state != KdState.BROKEN:
        raise NtDriveError(
            KD_NOT_BROKEN,
            f"target is {session.state}; kd_capture_fault needs a bugcheck break",
            "kd_wait_event catches a bugcheck, then call this",
        )
    # The processor the bugcheck landed on, before any command resets the implicit context.
    event = session.last_event or {}
    bc_processor = event.get("processor") if event.get("event") == "bugcheck" else None
    proc = p.processor if p.processor is not None else bc_processor

    prep: list[str] = []
    symbol_path = ""
    if p.cache_symbols:
        cache_dir = _local_symbol_cache(session.symbol_path)
        symbol_path = f"cache*{cache_dir}"
        prep.append(f".sympath {symbol_path}")
    if proc is not None:
        prep.append(f"~{proc}s")
    if prep:
        await session.exec(prep, timeout=p.timeout, max_bytes=4096)

    bc_text = (await session.exec([".bugcheck"], timeout=p.timeout, max_bytes=4096))[0]["output"]
    parsed = parse_bugcheck(bc_text)
    result: dict[str, Any] = {
        "vm": p.vm,
        "bugcheck": parsed,
        "processor": proc,
        "cache_symbols": p.cache_symbols,
    }
    if symbol_path:
        result["symbol_path"] = symbol_path
    if parsed is None:
        result["state"] = str(session.state)
        result["note"] = (
            ".bugcheck reported no code, so the target is not at a bugcheck. kd_capture_fault "
            "needs one (kd_wait_event catches it). For a plain break use kd_exec kb"
        )
        return result

    args = parsed["arguments"]
    rip_cmd = "u @rip L1"
    if p.context_record:
        await session.exec([f".cxr {p.context_record}"], timeout=p.timeout, max_bytes=2048)
        result["mode"] = "cxr"
        result["context_record"] = p.context_record
    else:
        fault_rip = p.fault_rip or (args[2] if len(args) >= 3 else None)
        if not fault_rip:
            result["state"] = str(session.state)
            result["note"] = (
                "could not determine the faulting RIP: the bugcheck has fewer than three "
                "arguments. Pass fault_rip=<addr> or context_record=<ptr>"
            )
            return result
        result["fault_rip"] = fault_rip
        search = await session.exec(
            [f"s -q @rsp L{p.search_quads:x} {fault_rip}"], timeout=p.timeout, max_bytes=8192
        )
        candidates = [hit - p.trap_offset for hit in _stack_search_hits(search[0]["output"])]
        result["trap_candidates"] = [f"{c:#x}" for c in candidates]
        if not candidates:
            result["state"] = str(session.state)
            result["note"] = (
                f"{fault_rip} was not found in {p.search_quads:#x} quadwords above rsp, so no "
                "KTRAP_FRAME was located. Try a larger search_quads, a different fault_rip "
                "(another bugcheck argument), or context_record"
            )
            return result
        if p.match_index >= len(candidates):
            result["state"] = str(session.state)
            result["note"] = (
                f"match_index {p.match_index} is out of range: only {len(candidates)} match(es). "
                "trap_candidates lists them"
            )
            return result
        trap_base = candidates[p.match_index]
        await session.exec([f".trap {trap_base:#x}"], timeout=p.timeout, max_bytes=2048)
        result["mode"] = "trap"
        result["trap_frame"] = f"{trap_base:#x}"
        rip_cmd = f"u {fault_rip} L1"

    outs = await session.exec(["kb", rip_cmd, "lm k"], timeout=p.timeout, max_bytes=16384)
    by_cmd = {str(o["cmd"]): str(o["output"]).strip() for o in outs}
    result["stack"] = by_cmd.get("kb", "")
    result["faulting_instruction"] = by_cmd.get(rip_cmd, "")
    result["modules"] = by_cmd.get("lm k", "")
    result["state"] = str(session.state)
    result["note"] = (
        "the register context is left at the fault (.trap/.cxr) for more kd_exec, and resets on "
        "the next break. Do not kd_detach or kd_go to leave: both resume and complete the crash"
    )
    service.state.record_event(p.vm, "kd_capture_fault", bugcheck=parsed.get("code"))
    return result


def _breakpoint_ids(listing: str) -> set[str]:
    """Breakpoint ids from `bl` output, whose first field is the id."""
    ids: set[str] = set()
    for line in listing.splitlines():
        m = re.match(r"\s*(\d+)\s", line)
        if m:
            ids.add(m.group(1))
    return ids


def _is_zero(text: str) -> bool:
    """True when a `?` evaluation printed zero, so the caller's condition is false."""
    m = re.search(r"Evaluate expression:\s*(-?\d+)", text)
    if m:
        return int(m.group(1)) == 0
    # `? <expr>` can print only the hex form, for example `00000000`00000000`.
    hexes = re.findall(r"\b[0-9a-fA-F`]+\b", text.replace("`", ""))
    return bool(hexes) and all(int(h, 16) == 0 for h in hexes if h)


@tool(
    "kd_sample",
    "Break on a symbol, let the target run, and collect the value of one or more expressions at "
    "each of the next n hits, then clear the breakpoint. Replaces a manual bp/g/eval round trip "
    "per hit. The breakpoint is plain and conditions are evaluated by the daemon, never compiled "
    "into the breakpoint, because a conditional breakpoint with gc on a hot function NMIs the "
    "guest.",
    SampleParams,
    positional=("vm", "symbol"),
    long_poll=True,
    effect="destructive",
)
async def kd_sample(service: NtDriveService, p: SampleParams) -> dict[str, Any]:
    """Sample a symbol n times. Needs a broken-in target, and leaves it broken in."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    started = time.monotonic()
    deadline = started + p.max_seconds

    def left() -> float:
        return deadline - time.monotonic()

    before = _breakpoint_ids((await session.exec(["bl"], timeout=p.timeout))[0]["output"])
    await session.exec([f"bp {p.symbol}"], timeout=p.timeout)
    after = _breakpoint_ids((await session.exec(["bl"], timeout=p.timeout))[0]["output"])
    mine = sorted(after - before)
    rows: list[dict[str, Any]] = []
    stopped = "n"
    skipped = 0
    try:
        while len(rows) < p.n:
            if left() <= 0:
                stopped = "max_seconds"
                break
            await session.go()
            event = await session.wait_event(timeout=min(p.timeout, max(left(), 1.0)))
            if event.get("event") == "timeout":
                stopped = "timeout"
                break
            if event.get("event") == "bugcheck":
                # The guest crashed under us. Report it with the code rather than looping.
                rows.append({"hit": len(rows) + 1, "bugcheck": event.get("bugcheck"), "values": {}})
                stopped = "bugcheck"
                break
            if p.condition:
                check = await session.exec([f"? {p.condition}"], timeout=p.timeout)
                if _is_zero(check[0]["output"]):
                    skipped += 1
                    continue
            values: dict[str, str] = {}
            if p.exprs:
                for out in await session.exec(list(p.exprs), timeout=p.timeout):
                    values[str(out["cmd"])] = str(out["output"])
            rows.append({"hit": len(rows) + 1, "values": values})
    finally:
        # Clear only the breakpoint this call set, so other breakpoints survive.
        with contextlib.suppress(NtDriveError):
            if mine:
                await session.exec([f"bc {' '.join(mine)}"], timeout=p.timeout)
    service.runtime(p.vm)
    service.state.record_event(p.vm, "kd_sample", symbol=p.symbol, hits=len(rows))
    return {
        "vm": p.vm,
        "symbol": p.symbol,
        "hits": len(rows),
        "rows": rows,
        "skipped_by_condition": skipped,
        "stopped_because": stopped,
        "elapsed_s": round(time.monotonic() - started, 1),
        "state": str(session.state),
    }


@tool(
    "kd_wait_event",
    "Wait until the running target stops (bugcheck, breakpoint, ...) or the timeout expires.",
    WaitParams,
    long_poll=True,
    effect="read",
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


@tool(
    "kd_state",
    "Debugger state: attached (kd.exe alive), state (detached, waiting, running, broken), "
    "transport, target info, last event and log path.",
    VmParams,
    effect="read",
)
async def kd_state(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Status."""
    service.vm_cfg(p.vm)
    session = service.kd_sessions.get(p.vm)
    if session is None:
        runtime = service.runtime(p.vm)
        return {
            "vm": p.vm,
            "attached": False,
            "state": "detached",
            "transport": runtime.kd_transport,
            "port": runtime.kd_port,
            "serial_pipe": runtime.kd_serial_pipe,
            "target_info": "",
            "last_event": None,
            "log_path": "",
            "pid": None,
            "note": "no debugger session for this VM yet: call kd_attach",
        }
    status = session.status()
    if status["attached"]:
        result = {"vm": p.vm, **status}
        if status["state"] == "broken":
            # `broken` is inferred from a kd> prompt in the transcript. If the target rebooted
            # underneath the debugger the prompt is stale, and commands then time out instead of
            # saying the target is gone. Say where the truth is rather than implying certainty.
            result["note"] = (
                "state broken is read from the last kd> prompt, so it can be stale if the target "
                "rebooted underneath the debugger. kd_go resyncs it, and vm_state probe=true says "
                "whether the guest is actually alive"
            )
        return result
    # kd.exe is gone. What the previous session saw (its target banner, its last break) must
    # not read as the present, so it moves under previous_session and the live fields go blank.
    previous = {"target_info": status["target_info"], "last_event": status["last_event"]}
    status.update(target_info="", last_event=None, pid=None)
    return {
        "vm": p.vm,
        **status,
        "previous_session": previous,
        "note": (
            "not attached: kd.exe is not running, so target_info and last_event are empty. "
            "previous_session holds what the last session saw and kd_log_tail has its "
            "transcript. Call kd_attach to attach again"
        ),
    }


@tool("kd_log_tail", "Last bytes of the kd.exe transcript.", LogTailParams, effect="read")
async def kd_log_tail(service: NtDriveService, p: LogTailParams) -> dict[str, Any]:
    """Transcript tail."""
    cfg = service.vm_cfg(p.vm)
    session = service.kd_session(cfg)
    return {"vm": p.vm, "text": session.log_tail(p.bytes), "log_path": str(session.log_path)}

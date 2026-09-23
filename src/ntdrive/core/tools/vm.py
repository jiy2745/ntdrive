"""vm_*: power state and lifecycle."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from ntdrive.config import KdnetConfig, add_vm_config, next_kdnet_port, remove_vm_config
from ntdrive.core.orchestrator import reboot_flow
from ntdrive.core.registry import tool
from ntdrive.core.state import PowerState
from ntdrive.core.tools.common import ConfirmMixin, NoParams, VmParams
from ntdrive.errors import INVALID_ARGS, SNAPSHOT_NOT_FOUND, NtDriveError

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

# A clone name is a config key and a folder name, so keep it to safe characters.
_CLONE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class DeleteParams(VmParams, ConfirmMixin):
    """vm_delete."""


class StartParams(VmParams):
    """vm_start."""

    gui: bool = Field(default=False, description="Show the VMware console window")
    discard_saved_state: bool = Field(
        default=False,
        description=(
            "Drop the saved (suspended) state the vmx still names and boot fresh from the disk. "
            "For a start that failed with reason saved_state_stale. The suspended memory is lost"
        ),
    )


class StopParams(VmParams, ConfirmMixin):
    """vm_stop."""

    mode: Literal["soft", "hard", "kill"] = Field(
        default="soft",
        description=(
            "soft asks the guest to shut down (it flushes its disks). hard cuts power. kill "
            "ends the VM's vmware-vmx process on the host and clears its lock files, for a VM "
            "that vmrun no longer controls. hard and kill need confirm=true"
        ),
    )


class RebootParams(VmParams, ConfirmMixin):
    """vm_reboot."""

    mode: Literal["soft", "hard", "kd"] = Field(
        default="soft",
        description=(
            "soft: shutdown /r in the guest. hard: hypervisor reset, needs confirm=true. "
            "kd: .reboot at kd>"
        ),
    )
    reattach_kd: bool = Field(default=True, description="Bring the debugger back after boot")
    reopen_term: bool = Field(default=True, description="Reopen dropped terminal sessions")
    timeout: float = Field(default=180, ge=1, description="Seconds to wait for the guest")


def _summary(
    service: NtDriveService, name: str, power: PowerState | NtDriveError
) -> dict[str, Any]:
    data = service.runtime(name).to_dict()
    data["backend"] = service.config.vms[name].backend
    if isinstance(power, NtDriveError):
        data["power_error"] = power.to_dict()["error"]
    return data


@tool(
    "vm_list",
    "List registered VMs with power, debugger and terminal state.",
    NoParams,
    positional=(),
    effect="read",
)
async def vm_list(service: NtDriveService, _: NoParams) -> dict[str, Any]:
    """Every VM in vms.yaml."""
    # One vmrun list for the whole call, not one per VM.
    powers = await service.refresh_powers(list(service.config.vms))
    vms = [_summary(service, name, powers[name]) for name in service.config.vms]
    return {"vms": vms}


class StateParams(VmParams):
    """vm_state."""

    probe: bool = Field(
        default=False,
        description=(
            "Also probe the guest and add guest_reachable: whether SSH answers now. Off by "
            "default because it costs a connection attempt; the kd block already tells running "
            "from broken/bugcheck without a probe"
        ),
    )


@tool("vm_state", "Power, debugger and terminal state of one VM.", StateParams, effect="read")
async def vm_state(service: NtDriveService, p: StateParams) -> dict[str, Any]:
    """Refresh and return one VM.

    power tells on from off; the kd block tells a running kernel from one halted at the debugger
    (state broken, last_event.event bugcheck with the code). probe=true adds guest_reachable for
    "is the desktop actually up" after a reboot, which power alone cannot answer.
    """
    cfg = service.vm_cfg(p.vm)  # an unknown or unsupported VM is an error here, not a power_error
    power = (await service.refresh_powers([p.vm]))[p.vm]
    summary = _summary(service, p.vm, power)
    if p.probe:
        if power == PowerState.RUNNING:
            try:
                reachable, _ = await service.ssh_reachable(cfg, timeout=10)
            except NtDriveError:
                reachable = False  # Tools reported no IP in time: not reachable yet
            summary["guest_reachable"] = reachable
        else:
            summary["guest_reachable"] = False
    return summary


class WaitReadyParams(VmParams):
    """vm_wait_ready."""

    timeout: float = Field(
        default=180, ge=1, description="Seconds to wait for the guest to answer SSH"
    )


@tool(
    "vm_wait_ready",
    "Wait until the guest is back up: block until SSH answers, or the timeout passes. For after a "
    "reboot or a bugcheck's auto-restart, so no manual polling loop is needed.",
    WaitReadyParams,
    long_poll=True,
    effect="read",
)
async def vm_wait_ready(service: NtDriveService, p: WaitReadyParams) -> dict[str, Any]:
    """Long-poll SSH reachability. Returns ready=false at the timeout rather than raising."""
    cfg = service.vm_cfg(p.vm)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + p.timeout
    ip = ""
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            # A short per-attempt bound so a guest that is still down (Tools report no IP) does
            # not eat the whole timeout in one blocking lookup: we retry until the deadline.
            reachable, ip = await service.ssh_reachable(cfg, timeout=min(remaining, 15))
        except NtDriveError:
            reachable = False  # no IP yet: still booting
        if reachable:
            service.state.record_event(p.vm, "wait_ready", ready=True)
            waited = round(p.timeout - remaining, 1)
            return {"vm": p.vm, "ready": True, "ip": ip, "waited_s": waited}
        await asyncio.sleep(min(2.0, max(0.1, deadline - loop.time())))
    return {
        "vm": p.vm,
        "ready": False,
        "ip": ip,
        "waited_s": round(p.timeout, 1),
        "note": (
            "SSH did not answer in time. The guest may still be booting, sitting at a login or "
            "BSOD (con_screenshot method=vnc), or SSH may be off (sys_health)"
        ),
    }


@tool(
    "vm_start",
    "Power on (or resume) a VM without the GUI by default. discard_saved_state boots fresh "
    "when a stale saved state blocks the resume.",
    StartParams,
    effect="additive",
)
async def vm_start(service: NtDriveService, p: StartParams) -> dict[str, Any]:
    """Start the VM, after dropping a stale saved state when asked to."""
    cfg = service.vm_cfg(p.vm)
    adapter = service.adapter_for(cfg)
    result: dict[str, Any] = {"vm": p.vm}
    if p.discard_saved_state:
        if await service.refresh_power(cfg) == PowerState.RUNNING:
            raise NtDriveError(
                INVALID_ARGS, f"VM {p.vm} is running, there is no saved state to discard"
            )
        result["saved_state_dropped"] = await adapter.discard_saved_state(cfg)
    await adapter.start(cfg, gui=p.gui)
    power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "start", discard_saved_state=p.discard_saved_state)
    result["power"] = str(power)
    return result


@tool(
    "vm_stop",
    "Stop the VM: mode soft, hard or kill. hard and kill need confirm=true.",
    StopParams,
    destructive=True,
    effect="destructive",
)
async def vm_stop(service: NtDriveService, p: StopParams) -> dict[str, Any]:
    """Stop the VM. Detaches the debugger first so the target is not left frozen."""
    cfg = service.vm_cfg(p.vm)
    released = await service.release_guest(p.vm)
    result: dict[str, Any] = {"vm": p.vm, "terms_dropped": released["terms_dropped"]}
    if p.mode == "kill":
        result.update(await service.adapter_for(cfg).kill(cfg))
        try:
            power = await service.refresh_power(cfg)
        except NtDriveError as exc:
            # vmrun may still be recovering; the kill itself is done, so report and go on.
            power = PowerState.UNKNOWN
            result["power_error"] = exc.to_dict()["error"]
    else:
        await service.adapter_for(cfg).stop(cfg, hard=(p.mode == "hard"))
        power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "stop", mode=p.mode)
    result["power"] = str(power)
    return result


@tool(
    "vm_reboot",
    "Reboot the guest (soft, hard or from the debugger) and bring kd and terminals back, the "
    "terminals under new session ids. hard needs confirm=true.",
    RebootParams,
    destructive=True,
    long_poll=True,
    effect="destructive",
)
async def vm_reboot(service: NtDriveService, p: RebootParams) -> dict[str, Any]:
    """Orchestrated reboot."""
    cfg = service.vm_cfg(p.vm)
    await service.ensure_running(cfg)
    return await reboot_flow(
        service,
        cfg,
        p.mode,
        reattach_kd=p.reattach_kd,
        reopen_term=p.reopen_term,
        timeout=p.timeout,
    )


@tool("vm_suspend", "Suspend the VM to disk.", VmParams, effect="additive")
async def vm_suspend(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Suspend. Terminal sessions are dropped and the debugger is detached."""
    cfg = service.vm_cfg(p.vm)
    released = await service.release_guest(p.vm)
    await service.adapter_for(cfg).suspend(cfg)
    power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "suspend")
    return {"vm": p.vm, "power": str(power), "terms_dropped": released["terms_dropped"]}


@tool("vm_resume", "Resume a suspended VM (same as vm_start).", VmParams, effect="additive")
async def vm_resume(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Resume."""
    cfg = service.vm_cfg(p.vm)
    await service.adapter_for(cfg).start(cfg)
    power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "resume")
    return {"vm": p.vm, "power": str(power)}


class ConfigParams(VmParams):
    """vm_config."""

    cpus: int | None = Field(
        default=None,
        ge=1,
        le=64,
        description=(
            "Virtual CPUs. Written as one socket with this many cores, which Windows client "
            "editions accept (they ignore CPUs beyond their socket limit)"
        ),
    )
    memory_mb: int | None = Field(
        default=None,
        ge=512,
        le=1048576,
        multiple_of=4,
        description="Guest RAM in MB, a multiple of 4",
    )
    nic: Literal["e1000e", "e1000", "vmxnet3"] | None = Field(
        default=None,
        description="Model of the first virtual NIC (ethernet0). KDNET needs e1000e",
    )


@tool(
    "vm_config",
    "Read or change the VM hardware in the vmx: cpus, memory_mb, nic. Without arguments it "
    "reports the current values. A change needs the VM powered off.",
    ConfigParams,
    effect="additive",
    idempotent=True,
)
async def vm_config(service: NtDriveService, p: ConfigParams) -> dict[str, Any]:
    """Hardware settings live in the vmx, which Workstation rewrites on power off."""
    cfg = service.vm_cfg(p.vm)
    adapter = service.adapter_for(cfg)
    changes = {
        key: value
        for key, value in (("cpus", p.cpus), ("memory_mb", p.memory_mb), ("nic", p.nic))
        if value is not None
    }
    before = await adapter.hardware(cfg)
    if not changes:
        return {"vm": p.vm, "hardware": before, "changed": []}
    result = await adapter.set_hardware(cfg, changes)
    service.state.record_event(p.vm, "config", changed=result["changed"])
    return {
        "vm": p.vm,
        "hardware": result["hardware"],
        "before": before,
        "changed": result["changed"],
    }


class CloneParams(VmParams):
    """vm_clone."""

    name: str = Field(description="Name for the new clone (its config key and vmx folder)")
    snapshot: str | None = Field(
        default=None, description="Source snapshot to clone from; the current one when omitted"
    )
    linked: bool = Field(
        default=True,
        description=(
            "Linked clone (shares the base disk, only its own changes take space) or a full copy. "
            "A running clone still uses its own RAM, so run only as many as the host has memory for"
        ),
    )


@tool(
    "vm_clone",
    "Clone a VM into a new registered VM, for giving each agent its own guest. A linked clone "
    "shares the base disk (cheap) but a running clone uses its own RAM. The clone gets its own "
    "KDNET port, so set its debugger on the guest (kd_setup_guest, reboot) before kd_attach.",
    CloneParams,
    positional=("vm", "name"),
    effect="additive",
)
async def vm_clone(service: NtDriveService, p: CloneParams) -> dict[str, Any]:
    """Create a clone from a snapshot and register it as a new VM in vms.yaml."""
    src = service.vm_cfg(p.vm)
    if not _CLONE_NAME.match(p.name):
        raise NtDriveError(
            INVALID_ARGS, "clone name may use letters, digits, dot, dash and underscore only"
        )
    if p.name in service.config.vms:
        raise NtDriveError(INVALID_ARGS, f"a VM named {p.name} is already registered")
    adapter = service.adapter_for(src)
    tree = await adapter.snapshot_list(src)
    snapshot = p.snapshot or tree.current
    if not snapshot:
        raise NtDriveError(
            INVALID_ARGS,
            f"{p.vm} has no snapshot to clone from",
            "snap_take a snapshot first, or pass snapshot=",
        )
    if snapshot not in tree.names():
        raise NtDriveError(
            SNAPSHOT_NOT_FOUND,
            f"{p.vm} has no snapshot named {snapshot}",
            f"known snapshots: {', '.join(tree.names()) or '(none)'}",
        )
    if p.linked and src.resolve_encryption_password():
        # vmrun cannot make a linked clone of an encrypted VM and misreports it as "already
        # running" (seen live). A full clone works because it copies and re-encrypts the disk.
        raise NtDriveError(
            INVALID_ARGS,
            f"{p.vm} is encrypted, and vmrun cannot make a linked clone of an encrypted VM",
            "pass linked=false for a full clone (it copies the whole disk, so it is slower and "
            "uses its own space)",
        )
    dst_vmx = str(Path(src.vmx).parent / "ntdrive-clones" / p.name / f"{p.name}.vmx")
    await adapter.clone(src, dst_vmx, p.name, snapshot, p.linked)
    clone = src.model_copy(deep=True)
    clone.name = p.name
    clone.vmx = dst_vmx
    clone.serial_pipe = ""  # re-derives from the new name
    if clone.kd_transport == "net":
        clone.kdnet = KdnetConfig(port=next_kdnet_port(service.config), key=clone.kdnet.key)
    add_vm_config(service.config, clone)
    service.state.record_event(p.name, "vm_clone", source=p.vm, linked=p.linked)
    return {
        "vm": p.name,
        "source": p.vm,
        "vmx": dst_vmx,
        "linked": p.linked,
        "snapshot": snapshot,
        "kd_transport": clone.kd_transport,
        "kdnet_port": clone.kdnet.port if clone.kd_transport == "net" else None,
        "note": (
            "the clone shares the base guest, so its accounts and disk match. It has its own KDNET "
            "port (net) or pipe (serial), but the guest still points at the base's, so run "
            "kd_setup_guest on the clone then vm_reboot mode=soft before kd_attach"
        ),
    }


@tool(
    "vm_delete",
    "Delete a VM and its files (a clone, usually), and drop its vms.yaml entry. Powers it off "
    "first. Needs confirm=true. When the VM's files are already gone (moved or deleted by hand) "
    "it just removes the stale entry. A base VM with linked clones cannot be deleted until the "
    "clones are gone.",
    DeleteParams,
    destructive=True,
    effect="destructive",
)
async def vm_delete(service: NtDriveService, p: DeleteParams) -> dict[str, Any]:
    """Detach the debugger, drop terminals, power off, delete the VM, drop its config entry.

    A stale entry whose vmx is already gone is just deregistered: vmrun cannot act on files that
    do not exist, so the config entry would otherwise be stuck (seen live after a VM was deleted
    in VMware but left in vms.yaml).
    """
    cfg = service.vm_cfg(p.vm)
    adapter = service.adapter_for(cfg)
    released = await service.release_guest(p.vm)
    files_present = bool(cfg.vmx) and Path(cfg.vmx).is_file()
    if files_present:
        if await service.refresh_power(cfg) != PowerState.OFF:
            await adapter.stop(cfg, hard=True)
        await adapter.delete_vm(cfg)
    remove_vm_config(service.config, p.vm)
    service.state.record_event(p.vm, "vm_delete", files_removed=files_present)
    return {
        "vm": p.vm,
        "deleted": True,
        "files_removed": files_present,
        "terms_dropped": released["terms_dropped"],
    }

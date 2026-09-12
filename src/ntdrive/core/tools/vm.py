"""vm_*: power state and lifecycle."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ntdrive.core.orchestrator import reboot_flow
from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.state import PowerState
from ntdrive.core.tools.common import ConfirmMixin, NoParams, VmParams
from ntdrive.errors import NtDriveError


class StartParams(VmParams):
    """vm_start."""

    gui: bool = Field(default=False, description="Show the VMware console window")


class StopParams(VmParams, ConfirmMixin):
    """vm_stop."""

    mode: Literal["soft", "hard"] = Field(
        default="soft", description="soft asks the guest to shut down; hard cuts power"
    )


class RebootParams(VmParams, ConfirmMixin):
    """vm_reboot."""

    mode: Literal["soft", "hard", "kd"] = Field(
        default="soft",
        description="soft: shutdown /r in the guest; hard: hypervisor reset; kd: .reboot at kd>",
    )
    reattach_kd: bool = Field(default=True, description="Bring the debugger back after boot")
    reopen_term: bool = Field(default=True, description="Reopen dropped terminal sessions")
    timeout: float = Field(default=180, ge=1, description="Seconds to wait for the guest")


async def _summary(service: NtDriveService, name: str) -> dict[str, Any]:
    cfg = service.vm_cfg(name)
    try:
        await service.refresh_power(cfg)
    except NtDriveError as exc:
        service.state.vm(name).power = PowerState.UNKNOWN
        runtime = service.runtime(name)
        data = runtime.to_dict()
        data["backend"] = cfg.backend
        data["power_error"] = exc.to_dict()["error"]
        return data
    runtime = service.runtime(name)
    data = runtime.to_dict()
    data["backend"] = cfg.backend
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
    vms = [await _summary(service, name) for name in service.config.vms]
    return {"vms": vms}


@tool("vm_state", "Power, debugger and terminal state of one VM.", VmParams, effect="read")
async def vm_state(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Refresh and return one VM."""
    return await _summary(service, p.vm)


@tool(
    "vm_start",
    "Power on (or resume) a VM without the GUI by default.",
    StartParams,
    effect="additive",
)
async def vm_start(service: NtDriveService, p: StartParams) -> dict[str, Any]:
    """Start the VM."""
    cfg = service.vm_cfg(p.vm)
    await service.adapter_for(cfg).start(cfg, gui=p.gui)
    power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "start")
    return {"vm": p.vm, "power": str(power)}


@tool(
    "vm_stop",
    "Shut the guest down (soft) or cut power (hard, needs confirm=true).",
    StopParams,
    destructive=True,
    effect="destructive",
)
async def vm_stop(service: NtDriveService, p: StopParams) -> dict[str, Any]:
    """Stop the VM. Detaches the debugger first so the target is not left frozen."""
    cfg = service.vm_cfg(p.vm)
    released = await service.release_guest(p.vm)
    await service.adapter_for(cfg).stop(cfg, hard=(p.mode == "hard"))
    power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "stop", mode=p.mode)
    return {"vm": p.vm, "power": str(power), "terms_dropped": released["terms_dropped"]}


@tool(
    "vm_reboot",
    "Reboot the guest (soft, hard or from the debugger) and bring kd and terminals back.",
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

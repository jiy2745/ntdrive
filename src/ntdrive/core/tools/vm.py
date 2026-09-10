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
)
async def vm_list(service: NtDriveService, _: NoParams) -> dict[str, Any]:
    """Every VM in vms.yaml."""
    vms = [await _summary(service, name) for name in service.config.vms]
    return {"vms": vms}


@tool("vm_state", "Power, debugger and terminal state of one VM.", VmParams)
async def vm_state(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Refresh and return one VM."""
    return await _summary(service, p.vm)


@tool("vm_start", "Power on (or resume) a VM without the GUI by default.", StartParams)
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


@tool("vm_suspend", "Suspend the VM to disk.", VmParams)
async def vm_suspend(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Suspend. Terminal sessions are dropped and the debugger is detached."""
    cfg = service.vm_cfg(p.vm)
    released = await service.release_guest(p.vm)
    await service.adapter_for(cfg).suspend(cfg)
    power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "suspend")
    return {"vm": p.vm, "power": str(power), "terms_dropped": released["terms_dropped"]}


@tool("vm_resume", "Resume a suspended VM (same as vm_start).", VmParams)
async def vm_resume(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """Resume."""
    cfg = service.vm_cfg(p.vm)
    await service.adapter_for(cfg).start(cfg)
    power = await service.refresh_power(cfg)
    service.state.record_event(p.vm, "resume")
    return {"vm": p.vm, "power": str(power)}

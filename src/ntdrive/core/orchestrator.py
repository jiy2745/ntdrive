"""Multi-step flows whose ordering the daemon guarantees: snapshot revert and reboot.

Each flow returns a `steps` list so the caller can see exactly what ran and what failed.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ntdrive.config import VmConfig
from ntdrive.core.state import KdState, PowerState
from ntdrive.errors import GUEST_FROZEN_BY_DEBUGGER, KD_NOT_BROKEN, NtDriveError

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService


class Steps:
    """Collects step results."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, **info: Any) -> None:
        """Record one step."""
        entry: dict[str, Any] = {"step": name, "ok": ok, "at": time.time()}
        entry.update(info)
        self.items.append(entry)

    async def run(self, name: str, coro: Any, **info: Any) -> Any:
        """Await a coroutine and record success or the error; errors propagate."""
        try:
            result = await coro
        except NtDriveError as exc:
            self.add(name, False, error=exc.to_dict()["error"], **info)
            raise
        self.add(name, True, **info)
        return result


def kd_configured(vm: VmConfig) -> bool:
    """True when the VM can be debugged without more setup: serial needs nothing, net a key."""
    return vm.kd_transport == "serial" or bool(vm.kdnet.key)


KD_NOT_CONFIGURED = "no kd transport configured (set kdnet.key or kd_transport: serial)"


async def _reattach_kd(
    service: NtDriveService, vm: VmConfig, steps: Steps, timeout: float
) -> dict[str, Any]:
    session = service.kd_session(vm)
    status: dict[str, Any] = await steps.run(
        "kd_attach", session.attach(wait_for_target=True, timeout=timeout)
    )
    return status


async def _reopen_terms(
    service: NtDriveService, vm: VmConfig, dropped: list[str], steps: Steps, timeout: float
) -> list[dict[str, Any]]:
    if not dropped:
        steps.add("term_reopen", True, sessions=[])
        return []
    ip = await steps.run("guest_ip", service.guest_ip(vm, timeout=timeout))
    sessions = await steps.run(
        "term_reopen", service.term.reopen(vm, ip, dropped, timeout), dropped=dropped
    )
    return [
        {"old": old, "new": new.session_id} for old, new in zip(dropped, sessions, strict=False)
    ]


async def revert_flow(
    service: NtDriveService,
    vm: VmConfig,
    name: str,
    *,
    start: bool,
    reattach_kd: bool,
    reopen_term: bool,
    timeout: float,
) -> dict[str, Any]:
    """Kd detach -> drop terminals -> revert -> start -> kd attach -> reopen terminals."""
    steps = Steps()
    adapter = service.adapter_for(vm)
    kd = service.kd_sessions.get(vm.name)
    was_attached = kd is not None and kd.attached
    if was_attached and kd is not None:
        await steps.run("kd_detach", kd.detach())
    else:
        steps.add("kd_detach", True, skipped="not attached")
    dropped = service.term.mark_disconnected(vm.name)
    await service.term.drop_transport(vm.name)
    steps.add("term_drop", True, sessions=dropped)
    await steps.run("snapshot_revert", adapter.snapshot_revert(vm, name), snapshot=name)
    runtime = service.state.vm(vm.name)
    runtime.current_snapshot = name
    if start:
        await steps.run("start", adapter.start(vm))
        runtime.power = PowerState.RUNNING
    else:
        runtime.power = await adapter.power_state(vm)
    kd_status: dict[str, Any] | None = None
    if start and reattach_kd and (was_attached or kd_configured(vm)):
        kd_status = await _reattach_kd(service, vm, steps, timeout)
    elif start and reattach_kd:
        steps.add("kd_attach", True, skipped=KD_NOT_CONFIGURED)
    else:
        steps.add("kd_attach", True, skipped="not requested")
    term_info: list[dict[str, Any]] = []
    if start and reopen_term:
        term_info = await _reopen_terms(service, vm, dropped, steps, timeout)
    else:
        steps.add("term_reopen", True, skipped="not requested")
    service.state.record_event(vm.name, "snapshot_revert", snapshot=name)
    return {"steps": steps.items, "kd": kd_status, "term": term_info, "power": str(runtime.power)}


async def _soft_shutdown(
    service: NtDriveService, vm: VmConfig, steps: Steps, transport: Any
) -> None:
    """`shutdown /r /t 0` over the existing SSH connection, else through guest tools."""
    adapter = service.adapter_for(vm)
    if transport is not None and hasattr(transport, "exec_once"):
        try:
            await transport.exec_once("shutdown.exe /r /t 0", timeout=30)
            steps.add("guest_shutdown", True, via="ssh")
            return
        except NtDriveError as exc:
            steps.add("guest_shutdown", False, via="ssh", error=exc.to_dict()["error"])
    await steps.run(
        "guest_shutdown",
        adapter.run_in_guest(vm, "C:\\Windows\\System32\\shutdown.exe", ["/r", "/t", "0"]),
        via="guest_tools",
    )


async def _kd_after_reboot(
    service: NtDriveService, vm: VmConfig, steps: Steps, timeout: float, kd_alive: bool
) -> dict[str, Any] | None:
    kd = service.kd_sessions.get(vm.name)
    if kd_alive and kd is not None:
        # The target looks for the debugger again early in boot. Keep kd.exe and wait for it to
        # announce the new connection (both KDNET and the serial pipe reconnect on their own).
        kd.state = KdState.WAITING
        reached = await kd._wait_state(  # noqa: SLF001
            {KdState.RUNNING, KdState.BROKEN}, timeout, allow_timeout=True
        )
        if reached or vm.kd_transport != "serial":
            steps.add("kd_reconnect", reached, state=str(kd.state))
            return kd.status()
        # A serial pipe can resync without a "Connected to" line. Respawn kd.exe to get to a
        # known state instead of reporting a stale one.
        steps.add("kd_reconnect", False, state=str(kd.state), retry="respawn")
        await steps.run("kd_detach", kd.detach(force=True))
        return await _reattach_kd(service, vm, steps, timeout)
    if kd_configured(vm):
        return await _reattach_kd(service, vm, steps, timeout)
    steps.add("kd_attach", True, skipped=KD_NOT_CONFIGURED)
    return None


async def reboot_flow(
    service: NtDriveService,
    vm: VmConfig,
    mode: str,
    *,
    reattach_kd: bool,
    reopen_term: bool,
    timeout: float,
) -> dict[str, Any]:
    """Reboot in one of three ways and bring the debugger and terminals back."""
    steps = Steps()
    adapter = service.adapter_for(vm)
    kd = service.kd_sessions.get(vm.name)
    kd_alive = kd is not None and kd.attached
    if mode in ("soft", "hard") and kd is not None and kd.state == KdState.BROKEN:
        raise NtDriveError(
            GUEST_FROZEN_BY_DEBUGGER,
            "the target is broken in; a soft or hard reboot would leave the debugger confused",
            "use mode=kd (.reboot from the debugger) or kd_go first",
        )
    if mode == "kd" and (kd is None or kd.state != KdState.BROKEN):
        raise NtDriveError(KD_NOT_BROKEN, "mode=kd needs a kd> prompt", "call kd_break first")
    # Keep the SSH connection until the shutdown command went through it, then drop it.
    transport = service.term.transport_for(vm)
    dropped = service.term.mark_disconnected(vm.name)
    steps.add("term_drop", True, sessions=dropped)
    if mode == "soft":
        await _soft_shutdown(service, vm, steps, transport)
    elif mode == "hard":
        await steps.run("reset", adapter.reset(vm, hard=True))
    else:
        assert kd is not None
        kd._write(".reboot\n")  # noqa: SLF001 - orchestrator drives the session directly
        kd.state = KdState.WAITING
        steps.add("kd_reboot", True)
    await service.term.drop_transport(vm.name)
    kd_status: dict[str, Any] | None = None
    if reattach_kd:
        kd_status = await _kd_after_reboot(service, vm, steps, timeout, kd_alive)
    else:
        steps.add("kd_attach", True, skipped="not requested")
    term_info: list[dict[str, Any]] = []
    if reopen_term:
        term_info = await _reopen_terms(service, vm, dropped, steps, timeout)
    else:
        steps.add("term_reopen", True, skipped="not requested")
    service.state.vm(vm.name).power = PowerState.RUNNING
    service.state.record_event(vm.name, "reboot", mode=mode)
    return {"steps": steps.items, "kd": kd_status, "term": term_info}

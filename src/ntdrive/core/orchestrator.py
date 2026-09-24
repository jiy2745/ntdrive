"""Multi-step flows whose ordering the daemon guarantees: snapshot revert and reboot.

Each flow returns a `steps` list so the caller can see exactly what ran and what failed.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ntdrive.config import VmConfig
from ntdrive.core.state import KdState, PowerState
from ntdrive.errors import KD_NOT_BROKEN, NtDriveError

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

# A soft reboot is confirmed by the guest going down within this grace, polled this often.
_REBOOT_VERIFY_GRACE = 30.0
_REBOOT_VERIFY_INTERVAL = 3.0


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
            exc.extra["steps"] = self.items  # what ran before the failure
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


async def _guest_boot_time(transport: Any) -> int | None:
    """The guest's last boot time as a FILETIME int over SSH, or None when it cannot be read."""
    if transport is None or not hasattr(transport, "exec_once"):
        return None
    try:
        _, out = await transport.exec_once(
            "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToFileTimeUtc()", timeout=20
        )
    except NtDriveError:
        return None
    line = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return int(line) if line.lstrip("-").isdigit() else None


async def _soft_reboot_took(transport: Any, before_boot: int) -> bool:
    """True once the guest goes down or returns with a newer boot time, False if it never does.

    `shutdown /r /t 0` can return success without rebooting (seen live on one VM). While the guest
    goes down SSH stops answering, which is the signal. A boot time unchanged through the whole
    grace means the command was a no-op and the caller should fall back to a hard reset.
    """
    deadline = time.monotonic() + _REBOOT_VERIFY_GRACE
    while True:
        boot = await _guest_boot_time(transport)
        if boot is None or boot > before_boot:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_REBOOT_VERIFY_INTERVAL)


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
        if reached:
            steps.add("kd_reconnect", True, state=str(kd.state))
            return kd.status()
        # No reconnection within the timeout. A serial pipe can resync without a "Connected to"
        # line, and a KDNET session left like this sits at [no_debuggee] until someone detaches
        # and attaches by hand (seen live after a hard reboot). Respawn kd.exe for a known state.
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
        # A frozen guest cannot run shutdown, and a reset under a broken-in debugger leaves kd
        # confused. The reboot is the orchestrated step, so it resumes the target itself.
        await steps.run("kd_go", kd.go(), reason="the target was broken in")
    if mode == "kd" and (kd is None or kd.state != KdState.BROKEN):
        raise NtDriveError(
            KD_NOT_BROKEN,
            "mode=kd needs a kd> prompt",
            "call kd_break first. If the guest crashed and kd shows [no_debuggee] (KDNET "
            "dropped), use vm_reboot mode=hard confirm=true, and vm_stop mode=kill confirm=true "
            "then vm_start when vmrun no longer answers",
        )
    # Keep the SSH connection until the shutdown command went through it, then drop it.
    transport = service.term.transport_for(vm)
    dropped = service.term.mark_disconnected(vm.name)
    steps.add("term_drop", True, sessions=dropped)
    if mode == "soft":
        before_boot = await _guest_boot_time(transport)
        await _soft_shutdown(service, vm, steps, transport)
        # shutdown /r can report success without rebooting, so confirm the guest actually went
        # down and fall back to a hard reset when it did not, instead of a silent no-op.
        if before_boot is not None and not await _soft_reboot_took(transport, before_boot):
            steps.add(
                "reboot_verify",
                False,
                reason="guest did not reboot within the grace",
                fallback="hard",
            )
            await steps.run("reset", adapter.reset(vm, hard=True), via="hard_fallback")
    elif mode == "hard":
        await steps.run("reset", adapter.reset(vm, hard=True))
        # `vmrun reset hard` can leave the VM POWERED OFF instead of resetting it (seen live on a
        # wedged guest). Verify instead of assuming: an unnoticed power-off sent the caller into a
        # full vm_wait_ready timeout against a dead VM.
        if await service.refresh_power(vm) != PowerState.RUNNING:
            steps.add(
                "reset_verify",
                False,
                reason="the reset left the VM powered off instead of resetting it",
                fallback="start",
            )
            await steps.run("start", adapter.start(vm), via="reset_fallback")
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
    # Record the power state that is actually true, not an assumption. This used to be a blind
    # `power = RUNNING`, so a reboot that ended with the VM off was reported as running and nothing
    # noticed until a later tool failed with "the virtual machine is not powered on".
    power = await service.refresh_power(vm)
    service.state.record_event(vm.name, "reboot", mode=mode)
    return {"steps": steps.items, "kd": kd_status, "term": term_info, "power": str(power)}

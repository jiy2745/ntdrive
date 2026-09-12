"""snap_*: snapshots with tree listing and orchestrated revert."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field

from ntdrive.config import VmConfig
from ntdrive.core.orchestrator import revert_flow
from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.state import PowerState
from ntdrive.core.tools.common import ConfirmMixin, VmParams
from ntdrive.errors import (
    BACKEND_ERROR,
    REASON_CONFIG_UNREADABLE,
    REASON_ENCRYPTED_LIVE,
    REASON_SNAPSHOT_MISSING,
    SNAPSHOT_NOT_FOUND,
    NtDriveError,
)

log = logging.getLogger("ntdrive.snap")

# vmrun errors that come and go for a second or two right after a suspend.
_TRANSIENT_REASONS = {REASON_CONFIG_UNREADABLE, REASON_SNAPSHOT_MISSING}


def _needs_suspend(exc: NtDriveError, allow: bool, power: PowerState) -> bool:
    """True when the failure is the encrypted-live case and the caller allowed the workaround."""
    return exc.reason == REASON_ENCRYPTED_LIVE and allow and power == PowerState.RUNNING


async def _suspend_run_resume(
    service: NtDriveService,
    cfg: VmConfig,
    op: Callable[[], Awaitable[None]],
    done: Callable[[], Awaitable[bool]],
) -> dict[str, Any]:
    """Suspend the VM, run a snapshot op until `done` is true, then always resume.

    Right after a suspend vmrun can briefly report that the vmx is unreadable, and a delete that
    actually succeeded can then report that the snapshot does not exist on a retry. So the real
    signal is the `done` predicate (checked against the snapshot list), not the op's return.

    Like vm_suspend this detaches the debugger and drops terminal sessions first, then reattaches
    the debugger after the resume when it was attached. A broken-in target is refused up front
    rather than silently resumed.
    """
    service.ensure_not_frozen(cfg.name)
    adapter = service.adapter_for(cfg)
    released = await service.release_guest(cfg.name)
    await adapter.suspend(cfg)
    failure: BaseException | None = None
    try:
        for attempt in range(6):
            try:
                await op()
            except NtDriveError as exc:
                if exc.reason not in _TRANSIENT_REASONS:
                    raise
            # The op may have raised a transient error after succeeding, so always ask.
            try:
                if await done():
                    break
            except NtDriveError as exc:
                if exc.reason not in _TRANSIENT_REASONS:
                    raise
            await asyncio.sleep(1.0 + attempt)
        else:
            raise NtDriveError(
                BACKEND_ERROR,
                "snapshot operation did not converge after suspend",
                "check the VMware UI; the VM has been resumed",
            )
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # Resume no matter what, but never let a resume failure hide the original error.
        try:
            await adapter.start(cfg)
        except NtDriveError as exc:
            if failure is None:
                raise
            log.warning("resume after a failed snapshot op also failed: %s", exc)
            if isinstance(failure, NtDriveError):
                failure.hint = (
                    f"{failure.hint} The VM may still be suspended (resume failed: "
                    f"{exc.message}); call vm_start."
                ).strip()
    service.state.vm(cfg.name).power = PowerState.RUNNING
    kd_status: dict[str, Any] | None = None
    if released["kd_was_attached"]:
        try:
            kd_status = await service.kd_session(cfg).attach(wait_for_target=True, timeout=120)
        except NtDriveError as exc:
            kd_status = {"state": "detached", "error": exc.to_dict()["error"]}
    return {
        "via": "suspend-resume",
        "terms_dropped": released["terms_dropped"],
        "kd": kd_status,
    }


class SnapNameParams(VmParams):
    """Tools that address one snapshot."""

    name: str = Field(description="Snapshot name")


class SnapTakeParams(SnapNameParams):
    """snap_take."""

    description: str = Field(default="", description="Free text stored with the snapshot")
    allow_suspend: bool = Field(
        default=False,
        description=(
            "If a live snapshot of a running encrypted VM is refused by vmrun, suspend the VM, "
            "snapshot the saved state (includes memory), then resume. Briefly pauses the guest, "
            "drops terminal sessions and reattaches the debugger afterwards."
        ),
    )


class SnapRevertParams(SnapNameParams):
    """snap_revert."""

    start: bool = Field(default=True, description="Power the VM on after the revert")
    reattach_kd: bool = Field(default=True, description="Reattach the kernel debugger")
    reopen_term: bool = Field(default=True, description="Reopen dropped terminal sessions")
    timeout: float = Field(default=180, ge=1, description="Seconds to wait for kd and ssh")


class SnapDeleteParams(SnapNameParams, ConfirmMixin):
    """snap_delete."""

    children: bool = Field(default=False, description="Also delete the whole subtree")
    allow_suspend: bool = Field(
        default=False,
        description=(
            "If deleting a memory snapshot of a running encrypted VM is refused by vmrun, suspend "
            "the VM, delete, then resume. Briefly pauses the guest, drops terminal sessions and "
            "reattaches the debugger afterwards."
        ),
    )


@tool(
    "snap_list",
    "Snapshot tree of a VM plus the current snapshot and stored metadata.",
    VmParams,
    effect="read",
)
async def snap_list(service: NtDriveService, p: VmParams) -> dict[str, Any]:
    """List snapshots."""
    cfg = service.vm_cfg(p.vm)
    tree = await service.adapter_for(cfg).snapshot_list(cfg)
    service.state.vm(p.vm).current_snapshot = tree.current
    data = tree.to_dict()
    data["metadata"] = service.load_snapshot_meta(p.vm)
    return data


@tool(
    "snap_take",
    "Take a snapshot (memory included while running) and record description and kd state.",
    SnapTakeParams,
    positional=("vm", "name"),
    effect="additive",
)
async def snap_take(service: NtDriveService, p: SnapTakeParams) -> dict[str, Any]:
    """Create a snapshot.

    vmrun refuses a live memory snapshot of a running encrypted VM. With allow_suspend the VM is
    suspended (its running state is written to the encrypted .vmss), snapshotted, then resumed,
    which captures the live state headlessly with only the encryption password.
    """
    cfg = service.vm_cfg(p.vm)
    adapter = service.adapter_for(cfg)
    power_before = await service.refresh_power(cfg)
    runtime = service.runtime(p.vm)
    kd_state_before = str(runtime.kd_state)
    detail: dict[str, Any] = {"via": "direct"}
    try:
        await adapter.snapshot_take(cfg, p.name)
    except NtDriveError as exc:
        if not _needs_suspend(exc, p.allow_suspend, power_before):
            raise

        # Suspend serializes memory into the encrypted .vmss, which vmrun can snapshot.
        async def present() -> bool:
            return p.name in (await adapter.snapshot_list(cfg)).names()

        detail = await _suspend_run_resume(
            service, cfg, lambda: adapter.snapshot_take(cfg, p.name), present
        )
    entry = {
        "description": p.description,
        "taken_at": time.time(),
        "kd_state_at_snapshot": kd_state_before,
        "power_at_snapshot": str(power_before),
        "via": detail["via"],
        "memory_included": power_before == PowerState.RUNNING,
    }
    meta = service.load_snapshot_meta(p.vm)
    meta[p.name] = entry
    service.save_snapshot_meta(p.vm, meta)
    runtime.current_snapshot = p.name
    service.state.record_event(p.vm, "snapshot_take", snapshot=p.name, via=detail["via"])
    return {"vm": p.vm, "name": p.name, **entry, **_extra(detail)}


@tool(
    "snap_revert",
    "Revert to a snapshot: detach kd, revert, start, reattach kd, reopen terminals.",
    SnapRevertParams,
    positional=("vm", "name"),
    long_poll=True,
    effect="destructive",
)
async def snap_revert(service: NtDriveService, p: SnapRevertParams) -> dict[str, Any]:
    """Orchestrated revert."""
    cfg = service.vm_cfg(p.vm)
    tree = await service.adapter_for(cfg).snapshot_list(cfg)
    if p.name not in tree.names():
        raise NtDriveError(
            SNAPSHOT_NOT_FOUND,
            f"VM {p.vm} has no snapshot named {p.name}",
            f"known snapshots: {', '.join(tree.names()) or '(none)'}",
        )
    return await revert_flow(
        service,
        cfg,
        p.name,
        start=p.start,
        reattach_kd=p.reattach_kd,
        reopen_term=p.reopen_term,
        timeout=p.timeout,
    )


@tool(
    "snap_delete",
    "Delete a snapshot (and optionally its children). Needs confirm=true.",
    SnapDeleteParams,
    positional=("vm", "name"),
    destructive=True,
    effect="destructive",
)
async def snap_delete(service: NtDriveService, p: SnapDeleteParams) -> dict[str, Any]:
    """Delete a snapshot."""
    cfg = service.vm_cfg(p.vm)
    adapter = service.adapter_for(cfg)
    before = await adapter.snapshot_list(cfg)
    if p.name not in before.names():
        raise NtDriveError(SNAPSHOT_NOT_FOUND, f"VM {p.vm} has no snapshot named {p.name}")
    power_before = await service.refresh_power(cfg)
    detail: dict[str, Any] = {"via": "direct"}
    try:
        await adapter.snapshot_delete(cfg, p.name, children=p.children)
    except NtDriveError as exc:
        if not _needs_suspend(exc, p.allow_suspend, power_before):
            raise

        # A memory snapshot of a running encrypted VM cannot be deleted in place, same as create.
        async def gone() -> bool:
            return p.name not in (await adapter.snapshot_list(cfg)).names()

        detail = await _suspend_run_resume(
            service,
            cfg,
            lambda: adapter.snapshot_delete(cfg, p.name, children=p.children),
            gone,
        )
    after = await adapter.snapshot_list(cfg)
    deleted = sorted(set(before.names()) - set(after.names()))
    meta = service.load_snapshot_meta(p.vm)
    for name in deleted:
        meta.pop(name, None)
    service.save_snapshot_meta(p.vm, meta)
    service.state.record_event(p.vm, "snapshot_delete", deleted=deleted, via=detail["via"])
    return {
        "vm": p.vm,
        "deleted": deleted,
        "current": after.current,
        "via": detail["via"],
        **_extra(detail),
    }


def _extra(detail: dict[str, Any]) -> dict[str, Any]:
    """Suspend-resume side effects worth reporting (dropped terminals, kd reattach result)."""
    return {k: v for k, v in detail.items() if k in ("terms_dropped", "kd")}

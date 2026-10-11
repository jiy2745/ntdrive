"""snap_*: snapshots with tree listing and orchestrated revert."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ntdrive.config import VmConfig
from ntdrive.core.orchestrator import revert_flow
from ntdrive.core.registry import tool
from ntdrive.core.state import PowerState
from ntdrive.core.tools.common import ConfirmMixin, VmParams
from ntdrive.errors import (
    BACKEND_ERROR,
    REASON_CONFIG_UNREADABLE,
    REASON_ENCRYPTED_LIVE,
    REASON_SNAPSHOT_EXISTS,
    REASON_SNAPSHOT_MISSING,
    SNAPSHOT_NOT_FOUND,
    NtDriveError,
)
from ntdrive.hypervisor.base import HypervisorAdapter

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

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
    # Pre-flight the encryption password BEFORE suspending, so a wrong or stale password can never
    # leave the VM suspended and unable to resume. "Authentication for encrypted virtual machine
    # failed" is the same text for a refused live snapshot (password fine) and a real auth failure,
    # so an authenticated read that is not refused on a running encrypted VM tells them apart:
    # listSnapshots succeeds when the password works, and fails when it does not.
    try:
        await adapter.snapshot_list(cfg)
    except NtDriveError as exc:
        raise NtDriveError(
            exc.code,
            f"cannot authenticate to the encrypted VM {cfg.name}, so it was not suspended: "
            f"{exc.message}",
            "check the encryption password (encryption_password_env in vms.yaml) and run "
            "`ntdrive daemon restart`, then retry. The VM is untouched and still running.",
            **({"reason": exc.reason} if exc.reason else {}),
        ) from exc
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
        resume_error = await _resume(adapter, cfg)
        if resume_error is not None and failure is not None:
            log.warning("resume after a failed snapshot op also failed: %s", resume_error)
            if isinstance(failure, NtDriveError):
                failure.hint = (
                    f"{failure.hint} The VM is not running either (resume failed: "
                    f"{resume_error.message}). Call vm_start."
                ).strip()
    detail: dict[str, Any] = {
        "via": "suspend-resume",
        "terms_dropped": released["terms_dropped"],
        "kd": None,
    }
    if resume_error is not None:
        # The snapshot work is done, the VM is not back. The caller finishes its bookkeeping
        # and then fails loudly with these facts instead of pretending the VM runs.
        power = PowerState.UNKNOWN
        with contextlib.suppress(NtDriveError):
            power = await service.refresh_power(cfg)
        detail["resume_error"] = resume_error.to_dict()["error"]
        detail["power"] = str(power)
        return detail
    service.state.vm(cfg.name).power = PowerState.RUNNING
    if released["kd_was_attached"]:
        try:
            detail["kd"] = await service.kd_session(cfg).attach(wait_for_target=True, timeout=120)
        except NtDriveError as exc:
            detail["kd"] = {"state": "detached", "error": exc.to_dict()["error"]}
    return detail


# Pause before the second resume attempt. Right after a snapshot of a suspended VM Workstation
# may still be rewriting files, and a first refusal has been seen to clear on its own.
_RESUME_RETRY_PAUSE = 3.0


async def _resume(adapter: HypervisorAdapter, cfg: VmConfig) -> NtDriveError | None:
    """`vmrun start` after the snapshot op, once more after a pause when the first try fails.

    The error of the last attempt is returned, never raised, so the caller can report the
    snapshot work that did succeed together with the state the VM was left in.
    """
    last: NtDriveError | None = None
    for attempt in range(2):
        try:
            await adapter.start(cfg)
            return None
        except NtDriveError as exc:
            last = exc
            log.warning("resume attempt %s failed: %s", attempt + 1, exc.message)
            if attempt == 0:
                await asyncio.sleep(_RESUME_RETRY_PAUSE)
    return last


def _raise_if_resume_failed(detail: dict[str, Any], done: str, result: dict[str, Any]) -> None:
    """The snapshot work succeeded but the VM did not come back: fail with the facts.

    `error.completed` is the result the call would have returned (the snapshot exists and is
    recorded), `error.power` is where the VM was left, `error.resume_error` is the start error
    with its own reason and hint (`saved_state_stale` names `vm_start discard_saved_state`).
    """
    err = detail.get("resume_error")
    if not err:
        return
    extra: dict[str, Any] = {
        "power": detail.get("power"),
        "resume_error": err,
        "completed": result,
    }
    if err.get("reason"):
        extra["reason"] = err["reason"]
    raise NtDriveError(
        BACKEND_ERROR,
        f"{done}, but the VM did not resume and is {detail.get('power')}: {err.get('message')}",
        err.get("hint")
        or "vm_start resumes it (the snapshot is safe), then term_open and kd_attach again",
        **extra,
    )


class SnapNameParams(VmParams):
    """Tools that address one snapshot."""

    name: str = Field(description="Snapshot name")


class SnapTakeParams(SnapNameParams):
    """snap_take."""

    description: str = Field(default="", description="Free text stored with the snapshot")
    replace: bool = Field(
        default=False,
        description=(
            "When the name already exists: false (default) reports the existing snapshot with "
            "created=false and changes nothing; true deletes it and takes a fresh one. On a "
            "running encrypted VM the delete joins the allow_suspend flow"
        ),
    )
    allow_suspend: bool = Field(
        default=False,
        description=(
            "When vmrun refuses a live snapshot of a running encrypted VM: suspend, snapshot "
            "the saved state (memory included), resume. Drops terminals, reattaches kd."
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
            "When vmrun refuses to delete a memory snapshot of a running encrypted VM: suspend, "
            "delete, resume. Drops terminals, reattaches kd."
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
    "Take a snapshot (memory included while running), record description and kd state, and "
    "return the snapshot list. A name that already exists is reported with created=false "
    "instead of erroring, and replace=true deletes it and retakes.",
    SnapTakeParams,
    positional=("vm", "name"),
    effect="additive",
)
async def snap_take(service: NtDriveService, p: SnapTakeParams) -> dict[str, Any]:
    """Create a snapshot.

    vmrun refuses a live memory snapshot of a running encrypted VM. With allow_suspend the VM is
    suspended (its running state is written to the encrypted .vmss), snapshotted, then resumed,
    which captures the live state headlessly with only the encryption password.

    A refused live take can still leave the snapshot in the tree (seen live 2026-10-02), so a
    retry used to hit "name already exists" with nobody able to tell which call captured the
    memory. Taking an existing name is therefore a report, not an error: created=false with the
    stored facts, or a note that it is an unrecorded leftover. replace=true is the explicit
    delete-and-retake.
    """
    cfg = service.vm_cfg(p.vm)
    adapter = service.adapter_for(cfg)
    power_before = await service.refresh_power(cfg)
    existing = False
    tree: Any = None
    try:
        tree = await adapter.snapshot_list(cfg)
        existing = p.name in tree.names()
    except NtDriveError:
        # The tree cannot be read (an encrypted-VM auth failure, say): fall through, the take
        # itself surfaces that error with its own classification.
        pass
    if existing and not p.replace:
        recorded = service.load_snapshot_meta(p.vm).get(p.name)
        result: dict[str, Any] = {
            "vm": p.vm,
            "name": p.name,
            "created": False,
            "snapshots": tree.names(),
            "current": tree.current,
        }
        if recorded:
            result.update(recorded)
            result["note"] = (
                "a snapshot with this name already exists and was left untouched; pass "
                "replace=true to delete it and take a fresh one"
            )
        else:
            result["note"] = (
                "a snapshot with this name exists but ntdrive never recorded creating it (left "
                "behind by a failed attempt), so whether it holds memory is unknown; pass "
                "replace=true to delete it and take a fresh one"
            )
        service.state.record_event(p.vm, "snapshot_take", snapshot=p.name, via="existing")
        return result

    runtime = service.runtime(p.vm)
    kd_state_before = str(runtime.kd_state)
    detail: dict[str, Any] = {"via": "direct"}

    async def present() -> bool:
        return p.name in (await adapter.snapshot_list(cfg)).names()

    try:
        if existing:
            await adapter.snapshot_delete(cfg, p.name)
        await adapter.snapshot_take(cfg, p.name)
    except NtDriveError as exc:
        if exc.reason != REASON_ENCRYPTED_LIVE:
            raise
        leftover = False
        if not existing:
            with contextlib.suppress(NtDriveError):
                leftover = await present()
        if leftover:
            raise NtDriveError(
                BACKEND_ERROR,
                f"vmrun refused the live snapshot of {p.vm} but left {p.name} in the tree, most "
                "likely without memory",
                "retry with allow_suspend=true and replace=true to delete the leftover and take "
                "a snapshot with memory included",
            ) from exc
        if not _needs_suspend(exc, p.allow_suspend, power_before):
            raise

        async def op() -> None:
            if existing:
                try:
                    await adapter.snapshot_delete(cfg, p.name)
                except NtDriveError as exc2:
                    if exc2.reason != REASON_SNAPSHOT_MISSING:
                        raise
            try:
                await adapter.snapshot_take(cfg, p.name)
            except NtDriveError as exc3:
                # A leftover from an earlier partial take makes vmrun refuse the name; `done`
                # (the tree) decides whether the flow already reached its goal.
                if exc3.reason != REASON_SNAPSHOT_EXISTS:
                    raise

        detail = await _suspend_run_resume(service, cfg, op, present)
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
    result = {"vm": p.vm, "name": p.name, "created": True, **entry, **_extra(detail)}
    if existing:
        result["replaced"] = True
    # The list proves the snapshot exists, so the caller need not call snap_list to check.
    with contextlib.suppress(NtDriveError):
        tree = await adapter.snapshot_list(cfg)
        result["snapshots"] = tree.names()
        result["current"] = tree.current
    _raise_if_resume_failed(detail, f"snapshot {p.name} was taken", result)
    return result


@tool(
    "snap_revert",
    "Revert to a snapshot: detach kd, revert, start, reattach kd, reopen terminals. The guest "
    "comes back running, not frozen at a kd> prompt, so the file_push and con_run that follow "
    "a revert work straight away (kd_break freezes it again when that is what you want).",
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
    kd_at_snapshot = service.load_snapshot_meta(p.vm).get(p.name, {}).get("kd_state_at_snapshot")
    result = await revert_flow(
        service,
        cfg,
        p.name,
        start=p.start,
        reattach_kd=p.reattach_kd,
        reopen_term=p.reopen_term,
        timeout=p.timeout,
    )
    if p.start and not p.reattach_kd and kd_at_snapshot in ("running", "broken", "waiting"):
        # This snapshot froze a guest that had the debugger attached. Restoring it with nothing on
        # the debug transport can leave the guest spinning to full CPU and unreachable, waiting for
        # a debugger that is not there. The flag is explicit, so this warns rather than overriding.
        result["warning"] = (
            f"this snapshot was taken while the debugger was attached "
            f"(kd_state_at_snapshot={kd_at_snapshot}) but reattach_kd=false, so nothing is driving "
            "the debug transport. The guest can spin to full CPU and go unreachable waiting for a "
            "debugger. If it does, kd_attach now, or snap_revert again with reattach_kd=true."
        )
    return result


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
    result = {
        "vm": p.vm,
        "deleted": deleted,
        "current": after.current,
        "via": detail["via"],
        **_extra(detail),
    }
    _raise_if_resume_failed(detail, f"snapshot {p.name} was deleted", result)
    return result


def _extra(detail: dict[str, Any]) -> dict[str, Any]:
    """Suspend-resume side effects worth reporting (dropped terminals, kd reattach result)."""
    return {k: v for k, v in detail.items() if k in ("terms_dropped", "kd")}

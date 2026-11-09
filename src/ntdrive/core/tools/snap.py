"""snap_*: snapshots with tree listing and orchestrated revert."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
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
    REASON_DISK_LOW,
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


# --- Retention -----------------------------------------------------------------------------------
# A snapshot chain grows on every guest write, so snapshots that nobody deletes fill the host
# drive. Each snapshot ntdrive takes is recorded as kept (keep=true, a base to come back to) or
# temporary (expires after host.snapshot_ttl_days). snap_prune deletes expired temporary ones.
# A snapshot with no recorded keep flag (taken before this existed, or outside ntdrive) is
# "unclassified" and is never pruned: ntdrive cannot tell whose it is, and several sessions can
# share one VM. snap_keep classifies it.

_GB = 1e9
_DAY = 86400.0


def disk_free(path: str) -> int:
    """Free bytes on the drive that holds `path`. A function so tests can replace it."""
    return shutil.disk_usage(os.path.dirname(path) or ".").free


def _vm_dir_bytes(vmx: str) -> int:
    """Bytes in the VM folder: base disk, delta disks and memory images together."""
    total = 0
    with contextlib.suppress(OSError):
        for entry in os.scandir(os.path.dirname(vmx) or "."):
            with contextlib.suppress(OSError):
                if entry.is_file():
                    total += entry.stat().st_size
    return total


def _classify(entry: dict[str, Any] | None, now: float) -> dict[str, Any]:
    """Retention facts for one snapshot from its recorded metadata."""
    if not entry or "keep" not in entry:
        return {"keep": None, "class": "unclassified"}
    out: dict[str, Any] = {"keep": bool(entry["keep"])}
    taken = entry.get("taken_at")
    if taken:
        out["age_days"] = round((now - float(taken)) / _DAY, 1)
    if entry["keep"]:
        out["class"] = "kept"
        return out
    expires = entry.get("expires_at")
    out["expires_in_days"] = round((float(expires) - now) / _DAY, 1) if expires else None
    out["class"] = "expired" if expires is not None and now >= float(expires) else "temporary"
    return out


def _retention(
    service: NtDriveService, cfg: VmConfig, names: list[str], current: str | None
) -> dict[str, Any]:
    """Disk facts plus the per-snapshot classification and the prune candidates."""
    now = time.time()
    meta = service.load_snapshot_meta(cfg.name)
    rows = {name: _classify(meta.get(name), now) for name in names}
    free = disk_free(cfg.vmx)
    minimum = service.config.host.snapshot_min_free_gb
    counts: dict[str, int] = {"kept": 0, "temporary": 0, "expired": 0, "unclassified": 0}
    for row in rows.values():
        counts[row["class"]] += 1
    # The current snapshot is what the VM runs from and is most likely the one being reverted
    # to, so it is never a candidate even when expired.
    candidates = [n for n, r in rows.items() if r["class"] == "expired" and n != current]
    disk: dict[str, Any] = {
        "host_free_gb": round(free / _GB, 1),
        "min_free_gb": minimum,
        "vm_folder_gb": round(_vm_dir_bytes(cfg.vmx) / _GB, 1),
        "snapshots": len(names),
        **counts,
        "prune_candidates": candidates,
    }
    return {"rows": rows, "disk": disk, "low": free / _GB < minimum, "free": free}


def _disk_note(disk: dict[str, Any]) -> str | None:
    """One line for a caller who should clean up soon, or None when there is nothing to say."""
    parts: list[str] = []
    if disk["host_free_gb"] < 2 * disk["min_free_gb"]:
        parts.append(
            f"the VM drive has {disk['host_free_gb']} GB free and snap_take refuses below "
            f"{disk['min_free_gb']} GB"
        )
    if disk["prune_candidates"]:
        parts.append(
            f"expired temporary snapshots: {', '.join(disk['prune_candidates'])} "
            "(snap_prune confirm=true deletes them)"
        )
    if disk["unclassified"]:
        parts.append(
            f"{disk['unclassified']} snapshot(s) have no keep flag and are never pruned: "
            "snap_keep marks each one kept or temporary"
        )
    return ". ".join(parts) if parts else None


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
    keep: bool = Field(
        default=False,
        description=(
            "true for a snapshot to come back to past this experiment (a clean base, a new "
            "build). false (default) makes it temporary: it expires after ttl_days and "
            "snap_prune may then delete it, whoever took it"
        ),
    )
    ttl_days: float | None = Field(
        default=None,
        gt=0,
        description="Days until a temporary snapshot expires (default host.snapshot_ttl_days)",
    )


class SnapRevertParams(SnapNameParams):
    """snap_revert."""

    start: bool = Field(default=True, description="Power the VM on after the revert")
    reattach_kd: bool = Field(default=True, description="Reattach the kernel debugger")
    reopen_term: bool = Field(default=True, description="Reopen dropped terminal sessions")
    discard_stale_saved_state: bool = Field(
        default=False,
        description=(
            "When the reverted vmx names a saved state it cannot restore (a .vmss that is not "
            "there, which repeated reverts of a suspend-resume memory snapshot can leave), drop it "
            "and boot anyway. Off by default because that boot is COLD: the snapshot's warm "
            "memory, the logged-in desktop and the kernel base are all lost"
        ),
    )
    timeout: float = Field(default=180, ge=1, description="Seconds to wait for kd and ssh")


class SnapKeepParams(SnapNameParams):
    """snap_keep."""

    keep: bool = Field(
        default=True,
        description="true marks it kept (never pruned), false marks it temporary",
    )
    ttl_days: float | None = Field(
        default=None,
        gt=0,
        description=(
            "With keep=false: days from now until it expires (default host.snapshot_ttl_days)"
        ),
    )


class SnapPruneParams(VmParams, ConfirmMixin):
    """snap_prune."""

    include_unexpired: bool = Field(
        default=False,
        description=(
            "Also delete temporary snapshots that have not expired yet (for a drive that is "
            "already short). Kept, unclassified and current snapshots are never touched"
        ),
    )
    allow_suspend: bool = Field(
        default=False,
        description=(
            "When vmrun refuses to delete a memory snapshot of a running encrypted VM: suspend, "
            "delete, resume. Drops terminals, reattaches kd."
        ),
    )


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
    "Snapshot tree of a VM plus the current snapshot and stored metadata. retention classes "
    "each snapshot kept, temporary, expired or unclassified, and disk gives the free space on "
    "the VM drive and the expired snapshots snap_prune would delete.",
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
    ret = _retention(service, cfg, tree.names(), tree.current)
    data["retention"] = ret["rows"]
    data["disk"] = ret["disk"]
    note = _disk_note(ret["disk"])
    if note:
        data["note"] = note
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
    _refuse_if_disk_low(service, cfg)
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
    taken_at = time.time()
    entry: dict[str, Any] = {
        "description": p.description,
        "taken_at": taken_at,
        "kd_state_at_snapshot": kd_state_before,
        "power_at_snapshot": str(power_before),
        "via": detail["via"],
        "memory_included": power_before == PowerState.RUNNING,
        "keep": p.keep,
    }
    if not p.keep:
        ttl = p.ttl_days or service.config.host.snapshot_ttl_days
        entry["expires_at"] = taken_at + ttl * _DAY
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
        # The caller reads this result at the moment it adds to the chain, which is when a
        # reminder to clean up actually lands.
        ret = _retention(service, cfg, tree.names(), tree.current)
        result["disk"] = ret["disk"]
        note = _disk_note(ret["disk"])
        if note:
            result["note"] = note
    _raise_if_resume_failed(detail, f"snapshot {p.name} was taken", result)
    return result


def _refuse_if_disk_low(service: NtDriveService, cfg: VmConfig) -> None:
    """snap_take on a nearly full drive: refuse and name what can be deleted."""
    free_gb = disk_free(cfg.vmx) / _GB
    minimum = service.config.host.snapshot_min_free_gb
    if free_gb >= minimum:
        return
    meta = service.load_snapshot_meta(cfg.name)
    now = time.time()
    temporary = sorted(
        (n for n, e in meta.items() if _classify(e, now)["class"] in ("expired", "temporary")),
        key=lambda n: meta[n].get("taken_at", 0),
    )
    hint = "snap_list shows each snapshot's class. "
    if temporary:
        hint += (
            f"Temporary snapshots, oldest first: {', '.join(temporary)}. snap_prune confirm=true "
            "deletes the expired ones (include_unexpired=true takes the rest too), or "
            "snap_delete one by name. "
        )
    else:
        hint += "There are no temporary snapshots to prune. "
    hint += (
        "Kept and unclassified snapshots are not yours to delete without asking the operator. "
        "host.snapshot_min_free_gb in vms.yaml sets the threshold"
    )
    raise NtDriveError(
        BACKEND_ERROR,
        f"the drive that holds {cfg.name} has {free_gb:.1f} GB free, below "
        f"host.snapshot_min_free_gb ({minimum} GB), so no snapshot was taken",
        hint,
        reason=REASON_DISK_LOW,
        free_gb=round(free_gb, 1),
    )


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
        discard_stale_saved_state=p.discard_stale_saved_state,
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


@tool(
    "snap_keep",
    "Mark a snapshot kept (never pruned) or temporary (expires, then snap_prune may delete it). "
    "Use it on a snapshot snap_list calls unclassified, or to pin a temporary one you still need.",
    SnapKeepParams,
    positional=("vm", "name"),
    effect="additive",
)
async def snap_keep(service: NtDriveService, p: SnapKeepParams) -> dict[str, Any]:
    """Record the keep flag in the snapshot metadata."""
    cfg = service.vm_cfg(p.vm)
    tree = await service.adapter_for(cfg).snapshot_list(cfg)
    if p.name not in tree.names():
        raise NtDriveError(
            SNAPSHOT_NOT_FOUND,
            f"VM {p.vm} has no snapshot named {p.name}",
            f"known snapshots: {', '.join(tree.names()) or '(none)'}",
        )
    meta = service.load_snapshot_meta(p.vm)
    entry = dict(meta.get(p.name) or {})
    entry["keep"] = p.keep
    entry.pop("expires_at", None)
    now = time.time()
    if not p.keep:
        ttl = p.ttl_days or service.config.host.snapshot_ttl_days
        entry["expires_at"] = now + ttl * _DAY
    meta[p.name] = entry
    service.save_snapshot_meta(p.vm, meta)
    service.state.record_event(p.vm, "snapshot_keep", snapshot=p.name, keep=p.keep)
    return {"vm": p.vm, "name": p.name, **_classify(entry, now)}


@tool(
    "snap_prune",
    "Delete the expired temporary snapshots of a VM (snap_list names them under "
    "disk.prune_candidates) and report the space it got back. Never touches kept, unclassified "
    "or current snapshots. Needs confirm=true.",
    SnapPruneParams,
    positional=("vm",),
    destructive=True,
    effect="destructive",
)
async def snap_prune(service: NtDriveService, p: SnapPruneParams) -> dict[str, Any]:
    """Delete temporary snapshots one at a time through snap_delete."""
    cfg = service.vm_cfg(p.vm)
    tree = await service.adapter_for(cfg).snapshot_list(cfg)
    before = _retention(service, cfg, tree.names(), tree.current)
    wanted = ("expired", "temporary") if p.include_unexpired else ("expired",)
    matching = [n for n, r in before["rows"].items() if r["class"] in wanted]
    targets = [n for n in matching if n != tree.current]
    deleted: list[str] = []
    failed: list[dict[str, Any]] = []
    for name in targets:
        try:
            out = await snap_delete(
                service,
                SnapDeleteParams(vm=p.vm, name=name, confirm=True, allow_suspend=p.allow_suspend),
            )
            deleted.extend(out["deleted"])
        except NtDriveError as exc:
            # One refused delete (an encrypted memory snapshot without allow_suspend, say) does
            # not stop the rest. The error says what to pass.
            failed.append({"name": name, "error": exc.to_dict()["error"]})
    free_after = disk_free(cfg.vmx)
    result: dict[str, Any] = {
        "vm": p.vm,
        "deleted": deleted,
        "failed": failed,
        "freed_gb": round((free_after - before["free"]) / _GB, 1),
        "host_free_gb": round(free_after / _GB, 1),
        "kept": [n for n, r in before["rows"].items() if r["class"] == "kept"],
        "unclassified": [n for n, r in before["rows"].items() if r["class"] == "unclassified"],
    }
    if tree.current in matching:
        result["skipped_current"] = tree.current
    if not targets:
        result["note"] = "nothing to prune: no temporary snapshot matched"
    return result


def _extra(detail: dict[str, Any]) -> dict[str, Any]:
    """Suspend-resume side effects worth reporting (dropped terminals, kd reattach result)."""
    return {k: v for k, v in detail.items() if k in ("terms_dropped", "kd")}

"""Snapshot retention (SNAP-5): keep flag, expiry, the disk check in snap_take, snap_prune."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.core.tools import snap
from ntdrive.errors import CONFIRM_REQUIRED, NtDriveError

from .conftest import FakeVmrun

VM = "win11-dev"
GB = 10**9


@pytest.fixture
def free(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Free bytes on the VM drive as the tools see it. Plenty unless a test lowers it."""
    box = {"bytes": 500 * GB}
    monkeypatch.setattr(snap, "disk_free", lambda path: box["bytes"])
    return box


def _expire(service: NtDriveService, *names: str) -> None:
    meta = service.load_snapshot_meta(VM)
    for name in names:
        meta[name]["expires_at"] = time.time() - 60
    service.save_snapshot_meta(VM, meta)


def _set_current(vmx: str, name: str) -> None:
    Path(vmx).with_suffix(".vmsd").write_text(
        f'snapshot.current = "7"\nsnapshot0.uid = "7"\nsnapshot0.displayName = "{name}"\n'
    )


async def test_snapshots_are_temporary_unless_kept(
    service: NtDriveService, free: dict[str, int]
) -> None:
    temp = await service.call("snap_take", {"vm": VM, "name": "try1"})
    assert temp["keep"] is False
    assert temp["expires_at"] == pytest.approx(time.time() + 7 * 86400, abs=60)
    assert temp["disk"]["host_free_gb"] == 500.0
    assert "note" not in temp

    kept = await service.call("snap_take", {"vm": VM, "name": "base", "keep": True})
    assert kept["keep"] is True and "expires_at" not in kept

    short = await service.call("snap_take", {"vm": VM, "name": "try2", "ttl_days": 1})
    assert short["expires_at"] == pytest.approx(time.time() + 86400, abs=60)

    listed = await service.call("snap_list", {"vm": VM})
    assert listed["retention"]["base"]["class"] == "kept"
    assert listed["retention"]["try1"]["class"] == "temporary"
    assert listed["disk"]["kept"] == 1 and listed["disk"]["temporary"] == 2


async def test_snap_take_refuses_on_a_full_drive_and_names_what_to_prune(
    service: NtDriveService, fake_vmrun: FakeVmrun, free: dict[str, int]
) -> None:
    await service.call("snap_take", {"vm": VM, "name": "old-try"})
    await service.call("snap_take", {"vm": VM, "name": "base", "keep": True})
    free["bytes"] = 12 * GB
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_take", {"vm": VM, "name": "new-try"})
    assert exc.value.reason == "host_disk_low"
    assert "old-try" in exc.value.hint and "snap_prune" in exc.value.hint
    assert "base" not in exc.value.hint.split("oldest first:")[1].split(".")[0]
    assert "new-try" not in [n for n, _ in fake_vmrun.snapshots]

    health = await service.call("sys_health", {})
    vm_issues = health["vms"][0]["issues"]
    assert any("snapshot_min_free_gb" in issue for issue in vm_issues)


async def test_snap_take_warns_while_there_is_still_room(
    service: NtDriveService, free: dict[str, int]
) -> None:
    await service.call("snap_take", {"vm": VM, "name": "a"})
    _expire(service, "a")
    free["bytes"] = 60 * GB  # above 40, below twice 40
    taken = await service.call("snap_take", {"vm": VM, "name": "b"})
    assert "60.0 GB free" in taken["note"]
    assert "a" in taken["disk"]["prune_candidates"]


async def test_snap_prune_deletes_only_expired_temporary_snapshots(
    service: NtDriveService, fake_vmrun: FakeVmrun, vmx_path: str, free: dict[str, int]
) -> None:
    for name, keep in (("expired", False), ("base", True), ("fresh", False), ("here", False)):
        await service.call("snap_take", {"vm": VM, "name": name, "keep": keep})
    await service.call("snap_take", {"vm": VM, "name": "legacy"})
    meta = service.load_snapshot_meta(VM)
    del meta["legacy"]["keep"]  # taken before the flag existed
    service.save_snapshot_meta(VM, meta)
    _expire(service, "expired", "here", "base")
    _set_current(vmx_path, "here")

    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_prune", {"vm": VM})
    assert exc.value.code == CONFIRM_REQUIRED

    listed = await service.call("snap_list", {"vm": VM})
    assert listed["disk"]["prune_candidates"] == ["expired"]
    assert listed["retention"]["legacy"]["class"] == "unclassified"

    out = await service.call("snap_prune", {"vm": VM, "confirm": True})
    assert out["deleted"] == ["expired"]
    assert out["skipped_current"] == "here"
    assert out["kept"] == ["base"] and out["unclassified"] == ["legacy"]
    left = [n for n, _ in fake_vmrun.snapshots]
    assert left == ["base", "fresh", "here", "legacy"]

    wider = await service.call("snap_prune", {"vm": VM, "include_unexpired": True, "confirm": True})
    assert wider["deleted"] == ["fresh"]
    assert [n for n, _ in fake_vmrun.snapshots] == ["base", "here", "legacy"]


async def test_snap_keep_classifies_any_snapshot(
    service: NtDriveService, free: dict[str, int]
) -> None:
    await service.call("snap_take", {"vm": VM, "name": "legacy"})
    service.save_snapshot_meta(VM, {})  # as if ntdrive never recorded it
    listed = await service.call("snap_list", {"vm": VM})
    assert listed["retention"]["legacy"]["class"] == "unclassified"
    assert "snap_keep" in listed["note"]

    kept = await service.call("snap_keep", {"vm": VM, "name": "legacy"})
    assert kept["class"] == "kept"
    temp = await service.call(
        "snap_keep", {"vm": VM, "name": "legacy", "keep": False, "ttl_days": 2}
    )
    assert temp["class"] == "temporary" and temp["expires_in_days"] == pytest.approx(2, abs=0.1)

    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_keep", {"vm": VM, "name": "nope"})
    assert exc.value.code == "snapshot_not_found"

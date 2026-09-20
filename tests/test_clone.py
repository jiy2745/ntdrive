"""vm_clone and vm_delete: give each agent its own guest, and clean it up."""

from __future__ import annotations

from pathlib import Path

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import NtDriveError

from .conftest import FakeVmrun


async def test_vm_clone_registers_a_new_vm_with_its_own_kdnet_port(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    result = await service.call(
        "vm_clone", {"vm": "win11-dev", "name": "agent2", "snapshot": "base"}
    )
    assert result["vm"] == "agent2" and result["source"] == "win11-dev" and result["linked"] is True
    # A clone command was issued and the clone's vmx now exists on the host.
    assert any("clone" in argv for argv in fake_vmrun.calls)
    assert Path(result["vmx"]).is_file()
    # The clone is a usable, registered VM with the base's account and its own KDNET port.
    assert "agent2" in service.config.vms
    clone = service.config.vms["agent2"]
    assert clone.guest.user == service.config.vms["win11-dev"].guest.user
    assert clone.kdnet.port != service.config.vms["win11-dev"].kdnet.port
    assert result["kdnet_port"] == clone.kdnet.port
    # It is persisted, so a reload sees it.
    from ntdrive.config import load_config

    assert "agent2" in load_config(service.config.path).vms


async def test_vm_clone_rejects_a_duplicate_name(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_clone", {"vm": "win11-dev", "name": "win11-dev", "snapshot": "base"})
    assert exc.value.code == "invalid_args"


async def test_vm_clone_needs_a_snapshot(service: NtDriveService, fake_vmrun: FakeVmrun) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_clone", {"vm": "win11-dev", "name": "agent3", "snapshot": "nope"})
    assert exc.value.code == "snapshot_not_found"


async def test_vm_delete_removes_the_vm_and_its_config(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    clone = await service.call(
        "vm_clone", {"vm": "win11-dev", "name": "agent4", "snapshot": "base"}
    )
    assert Path(clone["vmx"]).is_file()

    result = await service.call("vm_delete", {"vm": "agent4", "confirm": True})
    assert result["deleted"] is True
    assert "agent4" not in service.config.vms
    assert not Path(clone["vmx"]).exists()
    assert any("deleteVM" in argv for argv in fake_vmrun.calls)


async def test_vm_delete_needs_confirm(service: NtDriveService, fake_vmrun: FakeVmrun) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    await service.call("vm_clone", {"vm": "win11-dev", "name": "agent5", "snapshot": "base"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_delete", {"vm": "agent5"})
    assert exc.value.code == "confirm_required"

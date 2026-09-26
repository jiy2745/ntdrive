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


async def test_vm_clone_rejects_any_clone_of_an_encrypted_vm(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    service.config.vms["win11-dev"].encryption_password = "vmpw"  # now an encrypted source
    fake_vmrun.calls.clear()
    # Neither a linked nor a full clone: vmrun cannot clone an encrypted VM either way. A clear,
    # fast error, not vmrun's misleading "already running" / "cannot read config" chain.
    for linked in (True, False):
        with pytest.raises(NtDriveError) as exc:
            await service.call(
                "vm_clone",
                {"vm": "win11-dev", "name": "enc", "snapshot": "base", "linked": linked},
            )
        assert exc.value.code == "invalid_args" and "encrypted" in exc.value.message
        assert "GUI" in exc.value.hint and "snap_revert" in exc.value.hint
    # It failed fast: no clone was attempted and no VM was registered.
    assert not any("clone" in argv for argv in fake_vmrun.calls)
    assert "enc" not in service.config.vms


async def test_vm_clone_needs_a_snapshot(service: NtDriveService, fake_vmrun: FakeVmrun) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_clone", {"vm": "win11-dev", "name": "agent3", "snapshot": "nope"})
    assert exc.value.code == "snapshot_not_found"


async def test_vm_create_makes_an_unencrypted_clonable_vm(
    service: NtDriveService, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    """The escape from the encrypted base: a fresh VM vmrun CAN clone, because it has no key."""
    (tmp_path / "vmcli.exe").write_bytes(b"stub")  # sits next to vmrun
    service.config.vms["win11-dev"].encryption_password = "vmpw"  # template IS encrypted
    iso = tmp_path / "win11.iso"
    iso.write_bytes(b"ISO")
    result = await service.call(
        "vm_create",
        {"vm": "win11-dev", "name": "runner1", "iso": str(iso), "cpus": 4, "memory_mb": 8192},
    )
    assert result["encrypted"] is False and "runner1" in service.config.vms
    entry = service.config.vms["runner1"]
    # The encryption is deliberately NOT inherited, which is what makes vm_clone work on it later.
    assert entry.resolve_encryption_password() == ""
    assert entry.kdnet.port != service.config.vms["win11-dev"].kdnet.port
    assert entry.guest.user == service.config.vms["win11-dev"].guest.user
    # The vmx vmcli left incomplete is wired up: disk attached, UEFI, KDNET-capable NIC, ISO.
    text = Path(result["vmx"]).read_text(encoding="latin-1")
    assert 'nvme0:0.fileName = "runner1.vmdk"' in text
    assert 'firmware = "efi"' in text
    # Secure Boot is explicitly off, not left to the VMware default: there is no vTPM to back it,
    # and it is the other gate a Windows 11 install trips over.
    assert 'uefi.secureBoot.enabled = "FALSE"' in text
    assert 'ethernet0.virtualDev = "e1000e"' in text
    assert f'sata0:0.fileName = "{iso}"' in text and 'deviceType = "cdrom-image"' in text
    assert result["hardware"]["cpus"] == 4 and result["hardware"]["memory_mb"] == 8192
    # A clone of THIS one is allowed, unlike the encrypted template.
    await service.call("snap_take", {"vm": "runner1", "name": "base"})
    clone = await service.call("vm_clone", {"vm": "runner1", "name": "runner2", "snapshot": "base"})
    assert clone["vm"] == "runner2"


async def test_vm_create_rejects_a_missing_iso(
    service: NtDriveService, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    with pytest.raises(NtDriveError) as exc:
        await service.call(
            "vm_create", {"vm": "win11-dev", "name": "r9", "iso": str(tmp_path / "nope.iso")}
        )
    assert exc.value.code == "invalid_args" and "ISO" in exc.value.message
    assert "r9" not in service.config.vms


async def test_vm_register_adds_an_existing_vmx_with_a_fresh_kdnet_port(
    service: NtDriveService, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    # A VM cloned by hand or in the GUI (the only way for an encrypted VM): its vmx already exists.
    cloned = tmp_path / "gui-clone" / "gui-clone.vmx"
    cloned.parent.mkdir()
    cloned.write_text('displayName = "gui-clone"\n', encoding="utf-8")
    result = await service.call(
        "vm_register", {"vm": "win11-dev", "name": "agent7", "vmx": str(cloned)}
    )
    assert result["template"] == "win11-dev" and result["vmx"] == str(cloned)
    assert "agent7" in service.config.vms
    reg = service.config.vms["agent7"]
    # Inherits the template's guest and encryption config, gets its own KDNET port, no clone ran.
    assert reg.guest.user == service.config.vms["win11-dev"].guest.user
    assert reg.kdnet.port != service.config.vms["win11-dev"].kdnet.port
    assert not any("clone" in argv for argv in fake_vmrun.calls)
    # Persisted, so a reload sees it.
    from ntdrive.config import load_config

    assert "agent7" in load_config(service.config.path).vms


async def test_vm_register_rejects_a_missing_vmx(
    service: NtDriveService, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    with pytest.raises(NtDriveError) as exc:
        await service.call(
            "vm_register",
            {"vm": "win11-dev", "name": "agent8", "vmx": str(tmp_path / "nope.vmx")},
        )
    assert exc.value.code == "invalid_args" and "no vmx" in exc.value.message
    assert "agent8" not in service.config.vms


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
    assert result["deleted"] is True and result["files_removed"] is True
    assert "agent4" not in service.config.vms
    assert not Path(clone["vmx"]).exists()
    assert any("deleteVM" in argv for argv in fake_vmrun.calls)


async def test_vm_delete_drops_a_stale_entry_whose_files_are_gone(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    clone = await service.call(
        "vm_clone", {"vm": "win11-dev", "name": "agent6", "snapshot": "base"}
    )
    # The VM's files vanish (deleted in VMware, or moved), leaving only the vms.yaml entry.
    Path(clone["vmx"]).unlink()
    fake_vmrun.calls.clear()
    result = await service.call("vm_delete", {"vm": "agent6", "confirm": True})
    assert result["deleted"] is True and result["files_removed"] is False
    assert "agent6" not in service.config.vms
    # vmrun cannot act on missing files, so deleteVM is not attempted.
    assert not any("deleteVM" in argv for argv in fake_vmrun.calls)


async def test_vm_delete_needs_confirm(service: NtDriveService, fake_vmrun: FakeVmrun) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    await service.call("vm_clone", {"vm": "win11-dev", "name": "agent5", "snapshot": "base"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_delete", {"vm": "agent5"})
    assert exc.value.code == "confirm_required"

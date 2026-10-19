"""vm_create, vm_register and vm_delete: give an agent its own guest, and clean it up."""

from __future__ import annotations

from pathlib import Path

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import NtDriveError

from .conftest import FakeVmrun


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
    # The encryption is deliberately NOT inherited: that is the point of a fresh VM.
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


def _second_vmx(name: str, tmp: Path) -> Path:
    """A minimal vmx for a second VM, to be registered as vm_delete's subject."""
    vmx = tmp / name / f"{name}.vmx"
    vmx.parent.mkdir(parents=True, exist_ok=True)
    vmx.write_text('displayName = "old"\nnvram = "x.nvram"\n', encoding="latin-1")
    return vmx


async def test_vm_delete_removes_the_vm_and_its_config(
    service: NtDriveService, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    vmx = _second_vmx("agent4", tmp_path)
    clone = await service.call(
        "vm_register", {"vm": "win11-dev", "name": "agent4", "vmx": str(vmx)}
    )
    assert Path(clone["vmx"]).is_file()

    result = await service.call("vm_delete", {"vm": "agent4", "confirm": True})
    assert result["deleted"] is True and result["files_removed"] is True
    assert "agent4" not in service.config.vms
    assert not Path(clone["vmx"]).exists()
    assert any("deleteVM" in argv for argv in fake_vmrun.calls)


async def test_vm_delete_drops_a_stale_entry_whose_files_are_gone(
    service: NtDriveService, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    vmx = _second_vmx("agent6", tmp_path)
    clone = await service.call(
        "vm_register", {"vm": "win11-dev", "name": "agent6", "vmx": str(vmx)}
    )
    # The VM's files vanish (deleted in VMware, or moved), leaving only the vms.yaml entry.
    Path(clone["vmx"]).unlink()
    fake_vmrun.calls.clear()
    result = await service.call("vm_delete", {"vm": "agent6", "confirm": True})
    assert result["deleted"] is True and result["files_removed"] is False
    assert "agent6" not in service.config.vms
    # vmrun cannot act on missing files, so deleteVM is not attempted.
    assert not any("deleteVM" in argv for argv in fake_vmrun.calls)


async def test_vm_delete_needs_confirm(
    service: NtDriveService, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    vmx = _second_vmx("agent5", tmp_path)
    await service.call("vm_register", {"vm": "win11-dev", "name": "agent5", "vmx": str(vmx)})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_delete", {"vm": "agent5"})
    assert exc.value.code == "confirm_required"

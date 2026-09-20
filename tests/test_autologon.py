"""con_autologon: set and clear Windows automatic logon in the guest over SSH."""

from __future__ import annotations

import json

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import NtDriveError

from .conftest import FakeTransport, FakeVmrun


async def test_con_autologon_sets_the_winlogon_keys_without_leaking_the_password(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_log.clear()
    result = await service.call("con_autologon", {"vm": "win11-dev", "account": "admin"})
    assert result == {
        "vm": "win11-dev",
        "enabled": True,
        "account": "admin",
        "user": "dev",
        "needs_reboot": True,
    }
    assert "secret" not in json.dumps(result)  # the password never reaches the result
    script = fake_transport.exec_log[-1]
    assert "AutoAdminLogon -Value '1'" in script
    assert "DefaultUserName -Value 'dev'" in script
    assert "DefaultPassword -Value 'secret'" in script  # the guest command carries it, over SSH
    assert "secret" not in script[:40]  # written last, so no secret in the command's first chars


async def test_con_autologon_disable_clears_the_password(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_log.clear()
    result = await service.call("con_autologon", {"vm": "win11-dev", "enabled": False})
    assert result["enabled"] is False and result["user"] is None
    script = fake_transport.exec_log[-1]
    assert "AutoAdminLogon -Value '0'" in script
    assert "Remove-ItemProperty -Path $w -Name DefaultPassword" in script
    assert "secret" not in script


async def test_con_autologon_standard_without_an_account_is_an_error(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("con_autologon", {"vm": "win11-dev", "account": "standard"})
    assert exc.value.code == "invalid_args" and "standard_user" in exc.value.message

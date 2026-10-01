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
    # A leftover AutoLogonSID or a LogonUI SID hint outranks DefaultUserName, so they are cleared.
    assert "Remove-ItemProperty -Path $w -Name AutoLogonSID" in script
    assert "Remove-ItemProperty -Path $l -Name LastLoggedOnUserSID" in script
    assert "Remove-ItemProperty -Path $l -Name SelectedUserSID" in script


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
    assert "Remove-ItemProperty -Path $w -Name AutoLogonSID" in script
    assert "secret" not in script


async def test_con_autologon_standard_without_an_account_is_an_error(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("con_autologon", {"vm": "win11-dev", "account": "standard"})
    assert exc.value.code == "invalid_args" and "standard_user" in exc.value.message


async def test_con_run_executes_in_the_interactive_session_and_captures_output(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    # The guest returns the framed result the scheduled-task script prints.
    fake_transport.exec_responses["$ErrorActionPreference='Stop'"] = (
        "NTDRIVE_RC=0 STATE=Ready\nNTDRIVE_OUT_BEGIN\nhello from session 1\n"
    )
    result = await service.call("con_run", {"vm": "win11-dev", "cmd": "whoami", "account": "admin"})
    assert result["exit_code"] == 0 and result["state"] == "Ready"
    assert result["output"].strip() == "hello from session 1"
    script = fake_transport.exec_log[-1]
    assert "New-ScheduledTaskPrincipal -UserId 'dev' -LogonType Interactive" in script
    assert "-RunLevel Highest" in script  # admin runs elevated
    assert "cmd.exe" in script and "(whoami)" in script
    # The action sets a writable WorkingDirectory so a relative path does not resolve under
    # System32 (the scheduler default), where a standard account cannot write and the file is lost.
    assert "-WorkingDirectory 'C:\\Users\\Public'" in script
    assert "-Priority 7" in script  # below_normal by default, the long-standing behavior


async def test_con_run_honors_an_explicit_working_directory(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_responses["$ErrorActionPreference='Stop'"] = (
        "NTDRIVE_RC=0 STATE=Ready\nNTDRIVE_OUT_BEGIN\n"
    )
    await service.call(
        "con_run",
        {"vm": "win11-dev", "cmd": "run.bat", "account": "admin", "cwd": "C:\\work"},
    )
    assert "-WorkingDirectory 'C:\\work'" in fake_transport.exec_log[-1]


async def test_con_run_idle_priority_keeps_ssh_alive_under_a_cpu_bound_command(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_responses["$ErrorActionPreference='Stop'"] = (
        "NTDRIVE_RC=0 STATE=Ready\nNTDRIVE_OUT_BEGIN\n"
    )
    await service.call(
        "con_run",
        {"vm": "win11-dev", "cmd": "race.exe", "account": "admin", "priority": "idle"},
    )
    # Idle priority (task level 10) lets sshd and SFTP preempt a CPU-bound race loop, so a
    # breadcrumb file_pull or a taskkill still lands while it runs.
    assert "-Priority 10" in fake_transport.exec_log[-1]


async def test_con_run_detach_reports_dispatched_when_the_confirming_call_fails(
    service: NtDriveService,
    fake_transport: FakeTransport,
    fake_vmrun: FakeVmrun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A starved SSH return must not read as a failed launch: that drove a re-run and an orphan."""
    await service.call("vm_start", {"vm": "win11-dev"})
    from ntdrive.errors import TIMEOUT

    async def timing_out(command: str, timeout: float = 60.0) -> tuple[int, str]:
        raise NtDriveError(TIMEOUT, "the guest did not answer the SSH command within 60s")

    monkeypatch.setattr(fake_transport, "exec_once", timing_out)
    result = await service.call(
        "con_run",
        {"vm": "win11-dev", "cmd": "race.exe", "account": "admin", "detach": True},
    )
    # The task and log are known host-side, so the launch is reported as dispatched, not failed.
    assert result["detached"] is True and result["state"] == "unconfirmed"
    assert result["task"].startswith("ntdrive_run_") and result["log"].endswith(".log")
    assert "Do NOT re-run" in result["note"]


async def test_con_run_reports_a_command_that_did_not_finish(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_responses["$ErrorActionPreference='Stop'"] = (
        "NTDRIVE_RC=267009 STATE=Running\nNTDRIVE_OUT_BEGIN\n"
    )
    result = await service.call(
        "con_run", {"vm": "win11-dev", "cmd": "notepad", "account": "admin", "timeout": 2}
    )
    assert result["state"] == "Running" and "did not finish" in result["note"]


async def test_con_run_points_at_autologon_when_there_is_no_interactive_session(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    # The interactive task finished carrying a scheduler status (0x41303 = never ran), not a
    # program exit code, because no one is signed in on the desktop.
    fake_transport.exec_responses["$ErrorActionPreference='Stop'"] = (
        "NTDRIVE_RC=267011 STATE=Ready\nNTDRIVE_OUT_BEGIN\n"
    )
    result = await service.call("con_run", {"vm": "win11-dev", "cmd": "whoami", "account": "admin"})
    assert result["state"] == "Ready"
    assert "con_autologon" in result["note"] and "interactive desktop" in result["note"]


async def test_con_run_detach_starts_and_returns_at_once(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_responses["$ErrorActionPreference='Stop'"] = "NTDRIVE_RC=0 STATE=Running\n"
    result = await service.call(
        "con_run", {"vm": "win11-dev", "cmd": "provider.exe", "account": "admin", "detach": True}
    )
    assert result["detached"] is True and result["state"] == "Running"
    assert result["task"].startswith("ntdrive_run_") and result["log"].endswith(".log")
    # A detached task must not have the time limit that would kill a long-lived provider.
    script = fake_transport.exec_log[-1]
    assert "ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "Unregister-ScheduledTask" not in script  # left registered so the process is not stopped
    # The log is created up front so file_pull cannot race the task's own redirect.
    assert "New-Item -ItemType File -Path $log -Force" in script
    # A detached command gets the same writable WorkingDirectory as a foreground one.
    assert "-WorkingDirectory 'C:\\Users\\Public'" in script
    assert "-Priority 7" in script  # below_normal by default
    assert "exists now" in result["note"]


async def test_con_run_standard_without_an_account_is_an_error(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("con_run", {"vm": "win11-dev", "cmd": "whoami", "account": "standard"})
    assert exc.value.code == "invalid_args"

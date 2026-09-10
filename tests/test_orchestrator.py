from pathlib import Path

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import (
    BACKEND_ERROR,
    CONFIRM_REQUIRED,
    GUEST_FROZEN_BY_DEBUGGER,
    INVALID_ARGS,
    SNAPSHOT_NOT_FOUND,
    NtDriveError,
)

from .conftest import (
    FakeKdProcess,
    FakeTransport,
    FakeVmrun,
    always_reachable,
    never_reachable,
    settle,
)


def _calls(fake_vmrun: FakeVmrun, command: str) -> int:
    return sum(1 for call in fake_vmrun.calls if command in call)


async def test_snapshot_take_list_delete(service: NtDriveService, fake_vmrun: FakeVmrun) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    taken = await service.call(
        "snap_take", {"vm": "win11-dev", "name": "base", "description": "clean"}
    )
    assert taken["kd_state_at_snapshot"] == "detached"
    assert taken["via"] == "direct" and taken["memory_included"] is True
    listed = await service.call("snap_list", {"vm": "win11-dev"})
    assert listed["tree"][0]["name"] == "base"
    assert listed["metadata"]["base"]["description"] == "clean"
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_delete", {"vm": "win11-dev", "name": "base"})
    assert exc.value.code == CONFIRM_REQUIRED
    deleted = await service.call(
        "snap_delete", {"vm": "win11-dev", "name": "base", "confirm": True}
    )
    assert deleted["deleted"] == ["base"]
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_revert", {"vm": "win11-dev", "name": "base"})
    assert exc.value.code == SNAPSHOT_NOT_FOUND


async def test_snap_take_encrypted_live_needs_allow_suspend(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    """Live snapshot of a running encrypted VM: fail with a hint, or suspend-resume when allowed."""
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_vmrun.encrypted_live_snapshot_fails = True

    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_take", {"vm": "win11-dev", "name": "live1"})
    assert exc.value.code == BACKEND_ERROR
    assert exc.value.reason == "encrypted_live_snapshot"
    assert "allow_suspend" in exc.value.hint

    taken = await service.call(
        "snap_take", {"vm": "win11-dev", "name": "live1", "allow_suspend": True}
    )
    assert taken["via"] == "suspend-resume"
    assert taken["memory_included"] is True
    # The VM was suspended then resumed, so it is running again with the snapshot present.
    assert fake_vmrun.running and not fake_vmrun.suspended
    listed = await service.call("snap_list", {"vm": "win11-dev"})
    assert "live1" in [n["name"] for n in listed["tree"]]

    # Deleting that memory snapshot in place is refused too, and allow_suspend handles it.
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_delete", {"vm": "win11-dev", "name": "live1", "confirm": True})
    assert "allow_suspend" in exc.value.hint
    deleted = await service.call(
        "snap_delete",
        {"vm": "win11-dev", "name": "live1", "confirm": True, "allow_suspend": True},
    )
    assert deleted["deleted"] == ["live1"] and deleted["via"] == "suspend-resume"
    assert fake_vmrun.running and not fake_vmrun.suspended


async def test_suspend_resume_survives_transient_vmx_errors(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    """Right after a suspend vmrun may say the vmx is unreadable for a moment."""
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_vmrun.encrypted_live_snapshot_fails = True
    fake_vmrun.config_unreadable_after_suspend = 2
    taken = await service.call(
        "snap_take", {"vm": "win11-dev", "name": "flaky", "allow_suspend": True}
    )
    assert taken["via"] == "suspend-resume" and fake_vmrun.running
    assert "flaky" in [n for n, _ in fake_vmrun.snapshots]


async def test_suspend_resume_reports_resume_failure(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_vmrun.encrypted_live_snapshot_fails = True
    fake_vmrun.fail_start = True
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_take", {"vm": "win11-dev", "name": "x", "allow_suspend": True})
    # The snapshot itself worked; the error is the failed resume, and the VM is left suspended.
    assert exc.value.code == BACKEND_ERROR and "start" in exc.value.message
    assert fake_vmrun.suspended and "x" in [n for n, _ in fake_vmrun.snapshots]


async def test_suspend_resume_handles_debugger_and_terminals(
    service: NtDriveService,
    fake_vmrun: FakeVmrun,
    fake_transport: FakeTransport,
    kd_procs: list[FakeKdProcess],
) -> None:
    """allow_suspend must behave like vm_suspend: refuse while broken, drop terms, reattach kd."""
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_vmrun.encrypted_live_snapshot_fails = True
    opened = await service.call("term_open", {"vm": "win11-dev"})
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    first_proc = kd_procs[-1]
    await service.call("kd_break", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_take", {"vm": "win11-dev", "name": "s", "allow_suspend": True})
    assert exc.value.code == GUEST_FROZEN_BY_DEBUGGER
    assert fake_vmrun.running and not fake_vmrun.suspended  # nothing was suspended

    await service.call("kd_go", {"vm": "win11-dev"})
    taken = await service.call("snap_take", {"vm": "win11-dev", "name": "s", "allow_suspend": True})
    assert taken["via"] == "suspend-resume"
    assert taken["terms_dropped"] == [opened["session_id"]]
    assert taken["kd"]["state"] == "running"
    assert first_proc.poll() is not None and kd_procs[-1] is not first_proc
    assert service.state.term(opened["session_id"]).state == "disconnected"
    assert fake_transport.closed


async def test_revert_flow_restores_kd_and_terminal(
    service: NtDriveService,
    fake_vmrun: FakeVmrun,
    fake_transport: FakeTransport,
    kd_procs: list[FakeKdProcess],
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    opened = await service.call("term_open", {"vm": "win11-dev"})
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    first_proc = kd_procs[-1]

    import ntdrive.term.manager as manager_mod

    async def fake_wait(host: str, port: int, timeout: float, interval: float = 2.0) -> bool:
        return True

    manager_mod.wait_for_port = fake_wait  # type: ignore[assignment]

    result = await service.call("snap_revert", {"vm": "win11-dev", "name": "base", "timeout": 5})
    steps = [s["step"] for s in result["steps"]]
    assert steps == [
        "kd_detach",
        "term_drop",
        "snapshot_revert",
        "start",
        "kd_attach",
        "guest_ip",
        "term_reopen",
    ]
    assert all(s["ok"] for s in result["steps"])
    assert first_proc.poll() is not None  # old kd.exe was stopped
    assert kd_procs[-1] is not first_proc and result["kd"]["state"] == "running"
    assert result["term"][0]["old"] == opened["session_id"]
    new_sid = result["term"][0]["new"]
    old_info = service.state.term(opened["session_id"])
    assert old_info.state == "disconnected" and old_info.successor == new_sid
    assert fake_vmrun.running
    listing = await service.call("term_list", {})
    assert {s["session_id"] for s in listing["sessions"]} == {opened["session_id"], new_sid}


async def test_revert_and_reboot_reattach_serial_kd_without_key(
    service: NtDriveService, fake_vmrun: FakeVmrun, kd_procs: list[FakeKdProcess]
) -> None:
    """A serial VM has no KDNET key, but the debugger must still come back after revert/reboot."""
    cfg = service.config.vms["win11-dev"]
    cfg.kd_transport = "serial"
    cfg.kdnet.key = ""
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    result = await service.call(
        "snap_revert", {"vm": "win11-dev", "name": "base", "reopen_term": False, "timeout": 5}
    )
    attach = next(s for s in result["steps"] if s["step"] == "kd_attach")
    assert attach["ok"] and "skipped" not in attach
    assert result["kd"]["state"] == "running" and result["kd"]["transport"] == "serial"
    assert kd_procs[-1].argv[2].startswith("com:pipe,port=")

    # Reboot with kd alive: the serial session is kept and reconnects once kd prints the new
    # "Connected to" line, like it does when the rebooted target comes back.
    import asyncio

    proc = kd_procs[-1]
    asyncio.get_running_loop().call_later(0.3, proc.reconnect)
    result = await service.call(
        "vm_reboot",
        {"vm": "win11-dev", "mode": "hard", "confirm": True, "timeout": 5, "reopen_term": False},
    )
    reconnect = next(s for s in result["steps"] if s["step"] == "kd_reconnect")
    assert reconnect["ok"] and kd_procs[-1] is proc and result["kd"]["state"] == "running"

    # Same, but the serial pipe resyncs silently: after the wait kd.exe is respawned so the
    # reported state is a known one instead of a stale "waiting".
    result = await service.call(
        "vm_reboot",
        {"vm": "win11-dev", "mode": "hard", "confirm": True, "timeout": 1, "reopen_term": False},
    )
    names = [s["step"] for s in result["steps"]]
    assert names[-4:] == ["kd_reconnect", "kd_detach", "kd_attach", "term_reopen"]
    assert kd_procs[-1] is not proc and proc.poll() is not None
    assert result["kd"]["state"] == "running"

    # Reboot with kd detached: reattach happens because serial needs no key.
    await service.call("kd_detach", {"vm": "win11-dev"})
    result = await service.call(
        "vm_reboot",
        {"vm": "win11-dev", "mode": "hard", "confirm": True, "timeout": 5, "reopen_term": False},
    )
    attach = next(s for s in result["steps"] if s["step"] == "kd_attach")
    assert attach["ok"] and "skipped" not in attach and result["kd"]["state"] == "running"


async def test_reboot_refuses_while_broken_and_hard_needs_confirm(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_reboot", {"vm": "win11-dev", "mode": "hard"})
    assert exc.value.code == CONFIRM_REQUIRED
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_reboot", {"vm": "win11-dev", "mode": "soft"})
    assert exc.value.code == GUEST_FROZEN_BY_DEBUGGER
    proc = kd_procs[-1]
    result = await service.call(
        "vm_reboot", {"vm": "win11-dev", "mode": "kd", "reopen_term": False, "timeout": 1}
    )
    assert ".reboot" in proc.commands
    assert [s["step"] for s in result["steps"]][:2] == ["term_drop", "kd_reboot"]


async def test_soft_reboot_uses_existing_ssh_connection(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("term_open", {"vm": "win11-dev"})
    result = await service.call(
        "vm_reboot",
        {"vm": "win11-dev", "mode": "soft", "confirm": True, "reopen_term": False, "timeout": 1},
    )
    shutdown = next(s for s in result["steps"] if s["step"] == "guest_shutdown")
    assert shutdown["ok"] and shutdown["via"] == "ssh"
    assert "shutdown.exe /r /t 0" in fake_transport.exec_log
    assert _calls(fake_vmrun, "runProgramInGuest") == 0
    assert fake_transport.closed  # dropped after the command went through


async def test_file_push_and_pull(
    service: NtDriveService, fake_transport: FakeTransport, tmp_path: Path
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    src = tmp_path / "build"
    src.mkdir()
    (src / "mydrv.sys").write_bytes(b"MZ driver")
    (src / "mydrv.pdb").write_bytes(b"pdb")
    pushed = await service.call(
        "file_push", {"vm": "win11-dev", "local": str(src), "remote": "C:\\drv"}
    )
    assert pushed["files"] == 2 and pushed["verified"] == 2 and pushed["via"] == "ssh"
    assert fake_transport.files["C:\\drv\\mydrv.sys"] == b"MZ driver"
    pulled = await service.call(
        "file_pull",
        {"vm": "win11-dev", "remote": "C:\\drv\\mydrv.sys", "local": str(tmp_path / "out") + "\\"},
    )
    assert Path(pulled["local"]).read_bytes() == b"MZ driver"
    assert Path(pulled["local"]).name == "mydrv.sys"


async def test_file_tools_reject_relative_host_paths(service: NtDriveService) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("file_push", {"vm": "win11-dev", "local": "build", "remote": "C:\\d"})
    assert exc.value.code == INVALID_ARGS and "absolute" in exc.value.message
    with pytest.raises(NtDriveError) as exc:
        await service.call("file_pull", {"vm": "win11-dev", "remote": "C:\\a.txt", "local": "out/"})
    assert exc.value.code == INVALID_ARGS


async def test_file_push_reuses_live_ssh_connection(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    """A file copy must not re-resolve the IP or replace the transport that terminals use."""
    await service.call("vm_start", {"vm": "win11-dev"})
    opened = await service.call("term_open", {"vm": "win11-dev"})
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    ip_calls = _calls(fake_vmrun, "getGuestIPAddress")
    pushed = await service.call(
        "file_push", {"vm": "win11-dev", "local": str(src), "remote": "C:\\a.bin"}
    )
    assert pushed["via"] == "ssh"
    assert _calls(fake_vmrun, "getGuestIPAddress") == ip_calls
    assert not fake_transport.closed
    assert service.state.term(opened["session_id"]).state == "open"


async def test_file_push_falls_back_to_guest_tools_without_ssh(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    """Found in live testing: a guest with no OpenSSH must still get files via vmrun."""
    await service.call("vm_start", {"vm": "win11-dev"})
    src = tmp_path / "probe.ps1"
    src.write_bytes(b"Write-Output hi")

    service.ssh_probe = never_reachable
    pushed = await service.call(
        "file_push", {"vm": "win11-dev", "local": str(src), "remote": "C:\\ntdrive\\probe.ps1"}
    )
    assert pushed["via"] == "guest_tools"
    assert pushed["files"] == 1 and pushed["verified"] == 0
    assert pushed["copied"][0]["verified"] is None
    assert "guest tools" in pushed["note"]
    assert any("copyFileFromHostToGuest" in c for c in fake_vmrun.calls[-1])
    assert not fake_transport.files  # SFTP was never attempted

    # SFTP that dies mid-transfer must also fall back instead of raising on verification.
    service.ssh_probe = always_reachable
    fake_transport.fail_files = True
    pushed = await service.call(
        "file_push", {"vm": "win11-dev", "local": str(src), "remote": "C:\\ntdrive\\probe2.ps1"}
    )
    assert pushed["via"] == "guest_tools" and pushed["copied"][0]["verified"] is None
    assert "sftp failed" in pushed["note"]

    pulled = await service.call(
        "file_pull",
        {
            "vm": "win11-dev",
            "remote": "C:\\ntdrive\\probe.ps1",
            "local": str(tmp_path / "back.ps1"),
        },
    )
    assert pulled["via"] == "guest_tools" and "sftp failed" in pulled["note"]


async def test_file_push_reports_verification_failure(
    service: NtDriveService, fake_transport: FakeTransport, tmp_path: Path
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    fake_transport.fail_hash = True
    pushed = await service.call(
        "file_push", {"vm": "win11-dev", "local": str(src), "remote": "C:\\a.bin"}
    )
    assert pushed["via"] == "ssh" and pushed["verified"] == 0
    assert pushed["copied"][0]["verified"] is None
    assert "permission denied" in pushed["copied"][0]["verify_error"]
    assert "verification failed" in pushed["note"]


async def test_screenshot_and_audit(service: NtDriveService) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    shot = await service.call("con_screenshot", {"vm": "win11-dev", "base64": True})
    assert Path(shot["png_path"]).exists() and shot["png_base64"]
    await settle()
    audit = (service.log_dir / "audit.jsonl").read_text().splitlines()
    assert any('"tool": "con_screenshot"' in line for line in audit)
    assert all("secret" not in line for line in audit)

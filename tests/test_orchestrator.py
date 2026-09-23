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


async def test_allow_suspend_preflights_auth_and_never_suspends_on_a_bad_password(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    """A wrong or stale encryption password must abort before the suspend, not strand the VM."""
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_vmrun.encrypted_live_snapshot_fails = True  # snapshot_take reports the encrypted-auth text
    fake_vmrun.auth_fails = True  # but the password genuinely does not authenticate
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_take", {"vm": "win11-dev", "name": "live2", "allow_suspend": True})
    assert "authenticate" in exc.value.message.lower()
    assert "daemon restart" in exc.value.hint
    # The VM was never suspended, so it is still running and recoverable.
    assert not any("suspend" in argv for argv in fake_vmrun.calls)
    assert fake_vmrun.running and not fake_vmrun.suspended
    assert str(service.state.vm("win11-dev").power) == "running"


async def test_vm_wait_ready_returns_when_ssh_answers(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    result = await service.call("vm_wait_ready", {"vm": "win11-dev", "timeout": 5})
    assert result["ready"] is True and result["ip"]


async def test_vm_wait_ready_times_out_without_ssh(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    service.ssh_probe = never_reachable
    result = await service.call("vm_wait_ready", {"vm": "win11-dev", "timeout": 1})
    assert result["ready"] is False and "note" in result


async def test_vm_state_probe_reports_guest_reachable(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    up = await service.call("vm_state", {"vm": "win11-dev", "probe": True})
    assert up["guest_reachable"] is True
    # Without a probe the field is absent, so a plain state query pays no connection cost.
    plain = await service.call("vm_state", {"vm": "win11-dev"})
    assert "guest_reachable" not in plain
    service.ssh_probe = never_reachable
    down = await service.call("vm_state", {"vm": "win11-dev", "probe": True})
    assert down["guest_reachable"] is False


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("snap_take", {"vm": "win11-dev", "name": "base"})
    opened = await service.call("term_open", {"vm": "win11-dev"})
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    first_proc = kd_procs[-1]

    import ntdrive.term.manager as manager_mod

    async def fake_wait(host: str, port: int, timeout: float, interval: float = 2.0) -> bool:
        return True

    monkeypatch.setattr(manager_mod, "wait_for_port", fake_wait)

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


async def test_reboot_resumes_a_broken_target_and_hard_needs_confirm(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_reboot", {"vm": "win11-dev", "mode": "hard"})
    assert exc.value.code == CONFIRM_REQUIRED
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    # A soft reboot while broken in used to refuse with guest_frozen_by_debugger and send the
    # caller to kd_go. The orchestrated step resumes the target itself and says so.
    result = await service.call(
        "vm_reboot", {"vm": "win11-dev", "mode": "soft", "reopen_term": False, "timeout": 1}
    )
    names = [s["step"] for s in result["steps"]]
    assert names[0] == "kd_go" and "g" in proc.commands
    assert (await service.call("kd_state", {"vm": "win11-dev"}))["state"] == "running"

    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    result = await service.call(
        "vm_reboot", {"vm": "win11-dev", "mode": "kd", "reopen_term": False, "timeout": 1}
    )
    assert ".reboot" in proc.commands
    assert [s["step"] for s in result["steps"]][:2] == ["term_drop", "kd_reboot"]


async def test_net_reboot_respawns_kd_when_the_target_does_not_reconnect(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    """Seen live: after a hard reboot the kept KDNET session sat at [no_debuggee] and kd_break
    failed until the agent detached and attached by hand. Now a session that does not reconnect
    within the timeout is respawned, as the serial one already was."""
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    proc = kd_procs[-1]
    assert proc.argv[2].startswith("net:")
    result = await service.call(
        "vm_reboot",
        {"vm": "win11-dev", "mode": "hard", "confirm": True, "timeout": 1, "reopen_term": False},
    )
    names = [s["step"] for s in result["steps"]]
    assert names[-4:] == ["kd_reconnect", "kd_detach", "kd_attach", "term_reopen"]
    reconnect = next(s for s in result["steps"] if s["step"] == "kd_reconnect")
    assert reconnect["ok"] is False and reconnect["retry"] == "respawn"
    assert kd_procs[-1] is not proc and proc.poll() is not None
    assert result["kd"]["state"] == "running" and result["kd"]["attached"] is True


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


async def test_soft_reboot_falls_back_to_hard_when_the_guest_does_not_reboot(
    service: NtDriveService,
    fake_transport: FakeTransport,
    fake_vmrun: FakeVmrun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ntdrive.core.orchestrator as orch

    monkeypatch.setattr(orch, "_REBOOT_VERIFY_GRACE", 0.2)
    monkeypatch.setattr(orch, "_REBOOT_VERIFY_INTERVAL", 0.05)
    # The guest reports the same boot time throughout: shutdown /r was a no-op (seen live).
    fake_transport.exec_responses["(Get-CimInstance"] = "130000000000000\n"
    await service.call("vm_start", {"vm": "win11-dev"})
    await service.call("term_open", {"vm": "win11-dev"})
    result = await service.call(
        "vm_reboot",
        {"vm": "win11-dev", "mode": "soft", "confirm": True, "reopen_term": False, "timeout": 1},
    )
    verify = next(s for s in result["steps"] if s["step"] == "reboot_verify")
    assert verify["ok"] is False and verify["fallback"] == "hard"
    reset = next(s for s in result["steps"] if s["step"] == "reset")
    assert reset["ok"] and reset["via"] == "hard_fallback"
    assert _calls(fake_vmrun, "reset") == 1


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
    assert pushed["files"] == 2 and pushed["via"] == "ssh"
    assert pushed["verified"] is True and pushed["verified_count"] == 2
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
    # The fallback copy is hashed too: Get-FileHash ran in the guest and matched the host file.
    assert pushed["files"] == 1 and pushed["verified"] is True and pushed["verified_count"] == 1
    assert pushed["copied"][0]["verified"] is True
    assert "guest tools" in pushed["note"]
    assert any("copyFileFromHostToGuest" in c for c in fake_vmrun.calls)
    assert any("Get-FileHash" in " ".join(c) for c in fake_vmrun.calls)
    assert any("deleteFileInGuest" in c for c in fake_vmrun.calls)  # the report is cleaned up
    assert not fake_transport.files  # SFTP was never attempted

    # SFTP that dies mid-transfer must also fall back instead of raising on verification.
    service.ssh_probe = always_reachable
    fake_transport.fail_files = True
    pushed = await service.call(
        "file_push", {"vm": "win11-dev", "local": str(src), "remote": "C:\\ntdrive\\probe2.ps1"}
    )
    assert pushed["via"] == "guest_tools" and pushed["copied"][0]["verified"] is True
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
    assert pushed["via"] == "ssh" and pushed["verified"] is False and pushed["verified_count"] == 0
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


async def test_file_stat_ls_delete_over_sftp(
    service: NtDriveService, fake_transport: FakeTransport, tmp_path: Path
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    src = tmp_path / "log.txt"
    src.write_bytes(b"nine bytes")
    await service.call(
        "file_push", {"vm": "win11-dev", "local": str(src), "remote": "C:\\d\\log.txt"}
    )

    stat = await service.call("file_stat", {"vm": "win11-dev", "remote": "C:\\d\\log.txt"})
    assert stat["exists"] is True and stat["size"] == 10 and stat["is_dir"] is False
    assert stat["via"] == "ssh" and stat["modified"]
    missing = await service.call("file_stat", {"vm": "win11-dev", "remote": "C:\\d\\nope"})
    assert missing["exists"] is False and "size" not in missing

    listed = await service.call("file_ls", {"vm": "win11-dev", "remote": "C:\\d"})
    assert listed["exists"] is True and [e["name"] for e in listed["entries"]] == ["log.txt"]

    gone = await service.call("file_delete", {"vm": "win11-dev", "remote": "C:\\d\\log.txt"})
    assert gone["deleted"] is True and gone["via"] == "ssh"
    again = await service.call("file_delete", {"vm": "win11-dev", "remote": "C:\\d\\log.txt"})
    assert again["deleted"] is False  # already gone, reported not raised
    assert (await service.call("file_stat", {"vm": "win11-dev", "remote": "C:\\d\\log.txt"}))[
        "exists"
    ] is False


async def test_file_delete_falls_back_to_guest_tools_without_ssh(
    service: NtDriveService, fake_transport: FakeTransport, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_vmrun.guest_files["C:\\Users\\Public\\stale.txt"] = b"old"
    service.ssh_probe = never_reachable  # type: ignore[assignment]
    gone = await service.call(
        "file_delete", {"vm": "win11-dev", "remote": "C:\\Users\\Public\\stale.txt"}
    )
    assert gone["deleted"] is True and gone["via"] == "guest_tools"
    assert "C:\\Users\\Public\\stale.txt" not in fake_vmrun.guest_files


def test_guest_output_parsers() -> None:
    from ntdrive.hypervisor.vmware import parse_guest_entry, parse_guest_stat

    assert parse_guest_stat("9985|2026-09-14T01:02:03.0000000Z|False") == {
        "size": 9985,
        "modified": "2026-09-14T01:02:03.0000000Z",
        "is_dir": False,
    }
    assert parse_guest_stat("") is None
    assert parse_guest_entry("run.exe|512|2026-09-14T01:02:03Z|False")["name"] == "run.exe"
    assert parse_guest_entry("bad line") is None


async def test_snap_take_allow_suspend_reports_a_failed_resume(
    service: NtDriveService, fake_vmrun: FakeVmrun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot is taken and recorded, the VM is not back: the call fails with the facts."""
    import ntdrive.core.tools.snap as snap_mod

    monkeypatch.setattr(snap_mod, "_RESUME_RETRY_PAUSE", 0.0)
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_vmrun.encrypted_live_snapshot_fails = True
    fake_vmrun.fail_start = True
    with pytest.raises(NtDriveError) as exc:
        await service.call("snap_take", {"vm": "win11-dev", "name": "live2", "allow_suspend": True})
    err = exc.value
    assert err.code == "backend_error"
    assert "did not resume" in err.message
    assert err.extra["power"] == "suspended"
    assert err.extra["completed"]["name"] == "live2"
    assert "live2" in err.extra["completed"]["snapshots"]
    assert err.extra["resume_error"]["code"] == "backend_error"
    # Two start attempts, and the snapshot is on record as if the call had succeeded.
    assert sum(1 for argv in fake_vmrun.calls if "start" in argv) == 3
    assert "live2" in service.load_snapshot_meta("win11-dev")
    assert str(service.state.vm("win11-dev").power) == "suspended"


async def test_vm_start_names_a_stale_saved_state_and_discards_it(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    vmx = Path(service.config.vm("win11-dev").vmx)
    original = vmx.read_text(encoding="latin-1")
    vmx.write_text(
        original + 'checkpoint.vmState = "Snapshot5.vmsn"\ncheckpoint.vmState.readOnly = "FALSE"\n',
        encoding="latin-1",
    )
    health = await service.call("sys_health", {})
    issues = next(vm["issues"] for vm in health["vms"] if vm["name"] == "win11-dev")
    assert any("saved state" in issue for issue in issues)
    fake_vmrun.fail_start = True
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_start", {"vm": "win11-dev"})
    assert exc.value.reason == "saved_state_stale"
    assert "discard_saved_state" in exc.value.hint
    assert exc.value.extra["saved_state"] == "Snapshot5.vmsn"
    fake_vmrun.fail_start = False
    result = await service.call("vm_start", {"vm": "win11-dev", "discard_saved_state": True})
    assert result["power"] == "running"
    assert result["saved_state_dropped"]["saved_state"] == "Snapshot5.vmsn"
    assert len(result["saved_state_dropped"]["removed"]) == 2
    text = vmx.read_text(encoding="latin-1")
    assert "checkpoint" not in text.lower()
    assert text.startswith(original.rstrip("\n"))
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_start", {"vm": "win11-dev", "discard_saved_state": True})
    assert exc.value.code == "invalid_args"

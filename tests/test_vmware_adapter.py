import io
import sys
from pathlib import Path

import pytest

from ntdrive.config import Config
from ntdrive.core.service import NtDriveService
from ntdrive.core.state import PowerState
from ntdrive.errors import BACKEND_ERROR, TIMEOUT, VM_NOT_RUNNING, NtDriveError
from ntdrive.hostproc import force_utf8_stdio
from ntdrive.hypervisor import vmware as vmware_mod
from ntdrive.hypervisor.vmware import (
    VmwareAdapter,
    parse_current_snapshot,
    parse_snapshot_tree,
    subprocess_runner,
)
from ntdrive.hypervisor.vmx import apply_hardware, hardware_from_settings

from .conftest import FakeVmrun, never_reachable


async def test_subprocess_runner_captures_output_and_exit_code() -> None:
    # The real runner, with the no-window creation flag on Windows, still runs and reports.
    code, out = await subprocess_runner(
        [sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], 30
    )
    assert code == 3 and out.strip() == "hi"


def test_parse_snapshot_tree_nesting() -> None:
    out = "Total snapshots: 4\nbase\n\tchild\n\t\tgrandchild\nother\n"
    roots = parse_snapshot_tree(out)
    assert [r.name for r in roots] == ["base", "other"]
    assert roots[0].children[0].name == "child"
    assert roots[0].children[0].children[0].name == "grandchild"


def test_parse_current_snapshot_from_vmsd() -> None:
    vmsd = (
        '.encoding = "UTF-8"\n'
        'snapshot.current = "3"\n'
        'snapshot0.uid = "2"\n'
        'snapshot0.displayName = "base"\n'
        'snapshot1.uid = "3"\n'
        'snapshot1.displayName = "child"\n'
    )
    assert parse_current_snapshot(vmsd) == "child"
    assert parse_current_snapshot("") is None


async def test_power_state_and_snapshots(config: Config, fake_vmrun: FakeVmrun) -> None:
    adapter = VmwareAdapter(config.host.vmrun, runner=fake_vmrun)
    vm = config.vm("win11-dev")
    assert await adapter.power_state(vm) == PowerState.OFF
    await adapter.start(vm)
    assert await adapter.power_state(vm) == PowerState.RUNNING
    await adapter.snapshot_take(vm, "base")
    tree = await adapter.snapshot_list(vm)
    assert tree.names() == ["base"]
    await adapter.snapshot_revert(vm, "base")
    assert await adapter.power_state(vm) == PowerState.OFF
    await adapter.suspend(vm)
    assert await adapter.power_state(vm) == PowerState.SUSPENDED
    assert fake_vmrun.calls[1][:3] == [config.host.vmrun, "-T", "ws"]


async def test_read_commands_retry_and_errors(config: Config, fake_vmrun: FakeVmrun) -> None:
    adapter = VmwareAdapter(config.host.vmrun, runner=fake_vmrun, retries=3)
    vm = config.vm("win11-dev")
    fake_vmrun.fail_next["list"] = 2
    assert await adapter.power_state(vm) == PowerState.OFF
    with pytest.raises(NtDriveError) as exc:
        await adapter.snapshot_revert(vm, "missing")
    assert exc.value.code == BACKEND_ERROR
    assert "does not exist" in exc.value.message


async def test_guest_auth_is_inserted_for_tools_commands(
    config: Config, fake_vmrun: FakeVmrun, tmp_path: Path
) -> None:
    adapter = VmwareAdapter(config.host.vmrun, runner=fake_vmrun)
    vm = config.vm("win11-dev")
    # captureScreen is a VIX guest operation on Workstation, so it carries -gu/-gp.
    await adapter.screenshot(vm, str(tmp_path / "shot.png"))
    call = fake_vmrun.calls[-1]
    assert call[3:7] == ["-gu", "dev", "-gp", "secret"]
    assert call[7] == "captureScreen"


async def test_screenshot_explains_a_guest_login_failure(config: Config, tmp_path: Path) -> None:
    # When the guest login is broken, vmrun captureScreen fails; the hint says why and where to
    # look instead, rather than a bare backend error.
    async def bad_login(argv: list[str], timeout: float) -> tuple[int, str]:
        return 255, "Error: Invalid user name or password for the guest OS"

    adapter = VmwareAdapter(config.host.vmrun, runner=bad_login)
    with pytest.raises(NtDriveError) as exc:
        await adapter.screenshot(config.vm("win11-dev"), str(tmp_path / "shot.png"))
    assert "needs a working guest login" in exc.value.hint and "!analyze -v" in exc.value.hint


async def test_encrypted_vm_passes_vp(config: Config, fake_vmrun: FakeVmrun, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NTDRIVE_ENC", "sup3r-secret")
    vm = config.vm("win11-dev")
    vm.encryption_password_env = "NTDRIVE_ENC"
    adapter = VmwareAdapter(config.host.vmrun, runner=fake_vmrun)
    await adapter.start(vm)
    call = fake_vmrun.calls[-1]
    # -vp <password> must appear right after -T ws, before the command.
    assert call[3:5] == ["-vp", "sup3r-secret"]
    assert "start" in call
    assert fake_vmrun.saw_vp and fake_vmrun.vp_value == "sup3r-secret"


def test_apply_hardware_rewrites_only_the_touched_lines() -> None:
    text = 'displayName = "x"\nNumVCPUs = "2"\nmemsize = "4096"\nethernet0.virtualDev = "vmxnet3"\n'
    out, changed = apply_hardware(text, {"cpus": 4, "nic": "e1000e"})
    assert changed == ["numvcpus", "ethernet0.virtualDev", "cpuid.coresPerSocket"]
    # The existing key keeps its casing, memsize is untouched, coresPerSocket is appended.
    assert 'NumVCPUs = "4"' in out and 'memsize = "4096"' in out
    assert out.endswith('cpuid.coresPerSocket = "4"\n') and 'ethernet0.virtualDev = "e1000e"' in out
    again, changed = apply_hardware(out, {"cpus": 4, "nic": "e1000e"})
    assert again == out and changed == []
    hw = hardware_from_settings({"numvcpus": "4", "cpuid.corespersocket": "4", "memsize": "x"})
    assert hw == {"cpus": 4, "cores_per_socket": 4, "memory_mb": None, "nic": None}


async def test_vm_config_reads_and_writes_the_vmx_only_while_off(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun
) -> None:
    vm = config.vm("win11-dev")
    Path(vm.vmx).write_bytes(
        b'.encoding = "windows-1252"\ndisplayName = "caf\xe9"\nnumvcpus = "2"\n'
        b'memsize = "4096"\nethernet0.virtualDev = "vmxnet3"\n'
    )
    read = await service.call("vm_config", {"vm": "win11-dev"})
    assert read["hardware"] == {
        "cpus": 2,
        "cores_per_socket": None,
        "memory_mb": 4096,
        "nic": "vmxnet3",
    }
    assert read["changed"] == [] and "before" not in read

    done = await service.call("vm_config", {"vm": "win11-dev", "cpus": 4, "nic": "e1000e"})
    assert done["before"]["cpus"] == 2 and done["hardware"]["cpus"] == 4
    assert done["hardware"]["cores_per_socket"] == 4 and done["hardware"]["nic"] == "e1000e"
    assert done["changed"] == ["numvcpus", "ethernet0.virtualDev", "cpuid.coresPerSocket"]
    body = Path(vm.vmx).read_bytes()
    assert b'displayName = "caf\xe9"' in body  # the windows-1252 byte survived the rewrite
    assert b'memsize = "4096"' in body

    same = await service.call("vm_config", {"vm": "win11-dev", "cpus": 4})
    assert same["changed"] == []

    await service.call("vm_start", {"vm": "win11-dev"})
    still = await service.call("vm_config", {"vm": "win11-dev"})
    assert still["hardware"]["cpus"] == 4  # reading works while running
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_config", {"vm": "win11-dev", "memory_mb": 8192})
    assert exc.value.code == VM_NOT_RUNNING and "powered off" in exc.value.message
    with pytest.raises(NtDriveError) as bad:
        await service.call("vm_config", {"vm": "win11-dev", "memory_mb": 1001})
    assert bad.value.code == "invalid_args"


class _FakeProc:
    def __init__(self, pid: int, on_kill) -> None:  # type: ignore[no-untyped-def]
        self.pid = pid
        self.on_kill = on_kill
        self.waited: float | None = None

    def kill(self) -> None:
        self.on_kill()

    def wait(self, timeout: float | None = None) -> None:
        self.waited = timeout


async def test_vm_stop_kill_ends_the_vmx_process_and_clears_locks(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = config.vm("win11-dev")
    folder = Path(vm.vmx).parent
    (folder / "win11-dev.vmx.lck").mkdir()
    (folder / "win11-dev.vmx.lck" / "M12345.lck").write_text("pid")
    (folder / "disk.vmdk.lck").mkdir()
    (folder / "keep.txt").write_text("stays")
    await service.call("vm_start", {"vm": "win11-dev"})

    def gone() -> None:
        fake_vmrun.running = False

    proc = _FakeProc(4242, gone)
    monkeypatch.setattr(vmware_mod, "find_vmx_processes", lambda vmx: [proc])
    with pytest.raises(NtDriveError) as exc:
        await service.call("vm_stop", {"vm": "win11-dev", "mode": "kill"})
    assert exc.value.code == "confirm_required"  # a power cut, like hard
    done = await service.call("vm_stop", {"vm": "win11-dev", "mode": "kill", "confirm": True})
    assert done["killed"] == [4242] and proc.waited == 10
    assert done["locks_removed"] == ["disk.vmdk.lck", "win11-dev.vmx.lck"]
    assert done["power"] == "off" and "power_error" not in done
    assert not (folder / "win11-dev.vmx.lck").exists() and (folder / "keep.txt").exists()


async def test_vmrun_timeout_points_at_the_kill_path(config: Config) -> None:
    async def hung(argv: list[str], timeout: float) -> tuple[int, str]:
        raise NtDriveError(TIMEOUT, "vmrun.exe timed out after 180s")

    adapter = VmwareAdapter(config.host.vmrun, runner=hung)
    with pytest.raises(NtDriveError) as exc:
        await adapter.stop(config.vm("win11-dev"), hard=True)
    assert exc.value.code == TIMEOUT and exc.value.message.startswith("vmrun stop:")
    assert "vm_stop mode=kill" in exc.value.hint


def test_find_vmx_processes_matches_the_vmx_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class Info:
        def __init__(self, name: str, cmdline: list[str]) -> None:
            self.info = {"pid": 1, "name": name, "cmdline": cmdline}

    vmx = r"D:\VMs\win11\win11.vmx"
    procs = [
        Info(
            "vmware-vmx.exe",
            ["vmware-vmx.exe", "-s", "vmx.stdio.keep=TRUE", "d:/vms/win11/WIN11.vmx"],
        ),
        Info("vmrun.exe", ["vmrun.exe", "-T", "ws", "stop", vmx, "hard"]),
        Info("vmware-vmx.exe", ["vmware-vmx.exe", r"D:\VMs\other\other.vmx"]),
        Info("notepad.exe", [vmx]),
    ]
    monkeypatch.setattr(vmware_mod.psutil, "process_iter", lambda attrs: procs)
    found = vmware_mod.find_vmx_processes(vmx)
    assert [p.info["name"] for p in found] == ["vmware-vmx.exe", "vmrun.exe"]


async def test_file_pull_on_a_dead_guest_points_at_the_debugger(
    service: NtDriveService, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    service.ssh_probe = never_reachable  # type: ignore[assignment]
    adapter = service.adapter_for(config.vm("win11-dev"))

    async def dead(vm, remote, local):  # type: ignore[no-untyped-def]
        raise NtDriveError(BACKEND_ERROR, "vmrun copyFileFromGuestToHost failed: tools not running")

    monkeypatch.setattr(adapter, "copy_from_guest", dead)
    with pytest.raises(NtDriveError) as exc:
        await service.call(
            "file_pull",
            {"vm": "win11-dev", "remote": r"C:\Windows\MEMORY.DMP", "local": str(tmp_path / "d")},
        )
    assert exc.value.code == BACKEND_ERROR and "!analyze -v" in exc.value.hint


def test_force_utf8_stdio_reconfigures_redirected_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, encoding="cp949"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp949"))
    force_utf8_stdio()
    assert sys.stdout.encoding == "utf-8" and sys.stderr.encoding == "utf-8"
    sys.stdout.write("\u2603 symbols")  # a character cp949 cannot encode
    sys.stdout.flush()
    assert out.getvalue() == "\u2603 symbols".encode()


async def test_guest_ip_timeout_means_still_booting(config: Config) -> None:
    async def no_tools_yet(argv: list[str], timeout: float) -> tuple[int, str]:
        raise NtDriveError(TIMEOUT, "vmrun.exe timed out after 60s")

    adapter = VmwareAdapter(config.host.vmrun, runner=no_tools_yet)
    with pytest.raises(NtDriveError) as exc:
        await adapter.guest_ip(config.vm("win11-dev"), timeout=60)
    assert exc.value.code == TIMEOUT
    assert "still booting" in exc.value.hint
    assert not exc.value.hint.startswith("vmrun is not answering")


async def test_vm_list_runs_one_vmrun_list_for_every_vm(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    fake_vmrun.calls.clear()
    result = await service.call("vm_list", {})
    assert len(result["vms"]) == len(service.config.vms)
    assert sum(1 for argv in fake_vmrun.calls if "list" in argv) == 1

import sys
from pathlib import Path

import pytest

from ntdrive.config import Config
from ntdrive.core.state import PowerState
from ntdrive.errors import BACKEND_ERROR, NtDriveError
from ntdrive.hypervisor.vmware import (
    VmwareAdapter,
    parse_current_snapshot,
    parse_snapshot_tree,
    subprocess_runner,
)

from .conftest import FakeVmrun


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
    await adapter.screenshot(vm, str(tmp_path / "shot.png"))
    call = fake_vmrun.calls[-1]
    assert call[3:7] == ["-gu", "dev", "-gp", "secret"]
    assert call[7] == "captureScreen"


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

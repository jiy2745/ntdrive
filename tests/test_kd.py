from pathlib import Path

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import KD_NOT_ATTACHED, KD_NOT_BROKEN, NtDriveError
from ntdrive.kd.session import classify_break, generate_kdnet_key

from .conftest import FakeKdProcess, FakeVmrun, settle


def test_generate_kdnet_key_format() -> None:
    key = generate_kdnet_key()
    parts = key.split(".")
    assert len(parts) == 4
    assert all(p and all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in p) for p in parts)
    assert all(len(p) <= 13 for p in parts)


def test_argv_net_vs_serial() -> None:
    import asyncio

    from ntdrive.kd.session import KdSession

    loop = asyncio.new_event_loop()
    net = KdSession("vm", "kd.exe", 50000, "1.2.3.4", "sym", Path("x.log"), loop, transport="net")
    assert net.argv()[2] == "net:port=50000,key=1.2.3.4"
    ser = KdSession(
        "vm",
        "kd.exe",
        0,
        "",
        "sym",
        Path("x.log"),
        loop,
        transport="serial",
        serial_pipe=r"\\.\pipe\ntdrive-vm",
    )
    assert ser.argv()[2] == r"com:pipe,port=\\.\pipe\ntdrive-vm,baud=115200,resets=0,reconnect"
    loop.close()


async def test_ensure_serial_pipe(config, fake_vmrun) -> None:  # type: ignore[no-untyped-def]
    from ntdrive.errors import VM_NOT_RUNNING
    from ntdrive.hypervisor.vmware import VmwareAdapter

    vm = config.vm("win11-dev")
    # A windows-1252 byte (0xE9) in the display name must survive the rewrite untouched.
    Path(vm.vmx).write_bytes(b'.encoding = "windows-1252"\ndisplayName = "caf\xe9"\n')
    a = VmwareAdapter(config.host.vmrun, runner=fake_vmrun)
    pipe = vm.resolved_serial_pipe()
    assert pipe == r"\\.\pipe\ntdrive-win11-dev"
    assert await a.ensure_serial_pipe(vm, pipe) is True
    body = Path(vm.vmx).read_bytes()
    assert b'displayName = "caf\xe9"' in body
    assert f'serial0.fileName = "{pipe}"'.encode() in body
    assert b'serial0.pipe.endPoint = "server"' in body
    # Idempotent: a second call makes no change, even when the lines are reordered.
    assert await a.ensure_serial_pipe(vm, pipe) is False
    lines = body.decode("latin-1").splitlines()
    serial = [ln for ln in lines if ln.startswith("serial0.")]
    rest = [ln for ln in lines if not ln.startswith("serial0.")]
    Path(vm.vmx).write_text("\n".join(rest + list(reversed(serial))) + "\n", encoding="latin-1")
    assert await a.ensure_serial_pipe(vm, pipe) is False
    # The vmx is only rewritten while the VM is off.
    await a.start(vm)
    with pytest.raises(NtDriveError) as exc:
        await a.ensure_serial_pipe(vm, pipe)
    assert exc.value.code == VM_NOT_RUNNING


async def test_kd_setup_host_and_health_for_serial(
    service: NtDriveService, fake_vmrun: FakeVmrun
) -> None:
    cfg = service.config.vms["win11-dev"]
    cfg.kd_transport = "serial"
    cfg.kdnet.key = ""
    health = await service.call("sys_health", {})
    issues = health["vms"][0]["issues"]
    assert health["vms"][0]["kd_transport"] == "serial"
    assert any("serial pipe" in i for i in issues)
    assert not any("kdnet" in i for i in issues)  # net-only checks do not apply

    done = await service.call("kd_setup_host", {"vm": "win11-dev"})
    assert done["changed"] is True and done["serial_pipe"] == cfg.resolved_serial_pipe()
    health = await service.call("sys_health", {})
    assert health["vms"][0]["issues"] == []

    state = await service.call("kd_state", {"vm": "win11-dev"})
    assert state["transport"] == "serial" and state["port"] is None
    assert state["serial_pipe"] == cfg.resolved_serial_pipe()

    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_setup_host", {"vm": "win11-dev"})
    assert exc.value.code == "vm_not_running"


async def test_serial_attach_reports_dead_kd_and_missing_pipe(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    from ntdrive.errors import BACKEND_ERROR

    cfg = service.config.vms["win11-dev"]
    cfg.kd_transport = "serial"
    cfg.kdnet.key = ""
    service._kd_pipe_check = lambda pipe: False  # noqa: SLF001
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    assert exc.value.code == BACKEND_ERROR and "kd_setup_host" in exc.value.hint

    def dying(argv: list[str]) -> FakeKdProcess:
        proc = FakeKdProcess(argv)
        proc.inject(b"Cannot open pipe\r\n")
        proc.stop(1)
        return proc

    service.kd_sessions.clear()
    service._kd_pipe_check = lambda pipe: True  # noqa: SLF001
    service._kd_spawner = dying  # noqa: SLF001
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    assert exc.value.code == BACKEND_ERROR and "exited right after start" in exc.value.message
    assert (await service.call("kd_state", {"vm": "win11-dev"}))["state"] == "detached"


async def test_serial_attach_marks_running_and_captures_target_info(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    cfg = service.config.vms["win11-dev"]
    cfg.kd_transport = "serial"
    cfg.kdnet.key = ""
    attached = await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    assert attached["state"] == "running" and attached["transport"] == "serial"
    assert kd_procs[-1].argv[2].startswith("com:pipe,port=")
    await settle(0.2)
    # The "Connected to" line arrives after the attach over serial and is still captured.
    assert "Windows 11" in (await service.call("kd_state", {"vm": "win11-dev"}))["target_info"]
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_attach", {"vm": "win11-dev"})
    assert exc.value.code == "kd_already_attached"


def test_classify_break() -> None:
    assert classify_break("*** Fatal System Error: 0x7e") == "bugcheck"
    assert classify_break("Breakpoint 0 hit\nkd>") == "breakpoint"
    assert classify_break("Break instruction exception - code 80000003") == "user_break"
    assert classify_break("ModLoad: fffff800 mydrv.sys") == "module_load"
    assert classify_break("") == "unknown"


async def test_kd_lifecycle(service: NtDriveService, kd_procs: list[FakeKdProcess]) -> None:
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r"})
    assert exc.value.code == KD_NOT_ATTACHED

    attached = await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    assert attached["state"] == "running"
    assert "Windows 11" in attached["target_info"]
    proc = kd_procs[-1]
    assert proc.argv[1:3] == ["-k", "net:port=50000,key=1.2.3.4"]

    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r"})
    assert exc.value.code == KD_NOT_BROKEN

    broke = await service.call("kd_break", {"vm": "win11-dev"})
    assert broke["state"] == "broken"
    assert service.runtime("win11-dev").guest_frozen

    result = await service.call(
        "kd_exec", {"vm": "win11-dev", "cmd": "!process 0 0", "cmds": ["k"]}
    )
    outs = result["outputs"]
    assert [o["cmd"] for o in outs] == ["!process 0 0", "k"]
    assert outs[0]["output"] == "output of [!process 0 0]\nline two"
    assert not outs[0]["truncated"]
    assert any(c.startswith("!process 0 0; .echo __NTDRIVE_END_") for c in proc.commands)

    state = await service.call("kd_state", {"vm": "win11-dev"})
    assert state["state"] == "broken" and state["last_event"]["event"] == "user_break"

    assert (await service.call("kd_go", {"vm": "win11-dev"}))["state"] == "running"
    proc.bugcheck()
    event = await service.call("kd_wait_event", {"vm": "win11-dev", "timeout": 5})
    assert event["event"] == "bugcheck"
    assert "Fatal System Error" in event["output"]

    tail = await service.call("kd_log_tail", {"vm": "win11-dev", "bytes": 4000})
    assert "Fatal System Error" in tail["text"]

    detached = await service.call("kd_detach", {"vm": "win11-dev"})
    assert detached["state"] == "detached"
    assert proc.commands[-2:] == ["g", "q"]
    await settle()
    assert not service.runtime("win11-dev").guest_frozen


async def test_kd_setup_guest_writes_config(service: NtDriveService, fake_transport) -> None:  # type: ignore[no-untyped-def]
    await service.call("vm_start", {"vm": "win11-dev"})
    result = await service.call("kd_setup_guest", {"vm": "win11-dev", "port": 50005})
    assert result["needs_reboot"] and result["port"] == 50005
    assert fake_transport.exec_log[0] == "bcdedit /debug on"
    assert "hostip:192.168.126.1 port:50005 key:" in fake_transport.exec_log[1]
    assert service.config.vms["win11-dev"].kdnet.port == 50005
    key = service.config.vms["win11-dev"].kdnet.key
    assert key and "." in key
    with open(service.config.path, encoding="utf-8") as fh:
        saved = fh.read()
    # The port and the generated key are persisted, but the key never appears in the audit log.
    assert "50005" in saved and key in saved
    audit = (service.log_dir / "audit.jsonl").read_text()
    assert key not in audit

    # Serial: bcdedit serial settings, no key generated or saved.
    service.config.vms["win11-dev"].kd_transport = "serial"
    result = await service.call("kd_setup_guest", {"vm": "win11-dev"})
    assert result["transport"] == "serial" and result["port"] is None
    assert result["key_saved"] is False
    assert "serial debugport:1" in fake_transport.exec_log[-2]
    assert service.config.vms["win11-dev"].kdnet.key == key  # untouched

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import KD_NOT_ATTACHED, KD_NOT_BROKEN, TIMEOUT, NtDriveError
from ntdrive.kd.session import classify_break, generate_kdnet_key, parse_bugcheck

from .conftest import FakeKdProcess, FakeVmrun, settle

# Runs inside a detached helper process, the way ntdrived runs: a stand-in for kd.exe is spawned
# through spawn_kd and must exit through its SIGBREAK handler when send_ctrl_break fires.
BREAK_PROBE = r"""
import sys
from ntdrive.kd.session import send_ctrl_break, spawn_kd

CHILD = (
    "import signal, sys, time\n"
    "def stop(*args):\n"
    "    sys.stdout.write('GOT_BREAK\\n')\n"
    "    sys.stdout.flush()\n"
    "    sys.exit(3)\n"
    "signal.signal(signal.SIGBREAK, stop)\n"
    "sys.stdout.write('READY\\n')\n"
    "sys.stdout.flush()\n"
    # One long sleep is not interruptible by CTRL_BREAK on Windows: the handler would only run
    # after it ends, so the child sleeps in short slices.
    "for _ in range(200):\n"
    "    time.sleep(0.1)\n"
)
proc = spawn_kd([sys.executable, "-c", CHILD])
assert proc.stdout.readline().strip() == b"READY"
send_ctrl_break(proc)
rest = proc.stdout.read().decode()
print("rest", rest.strip(), "rc", proc.wait(timeout=15))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="CTRL_BREAK delivery is Windows only")
def test_ctrl_break_reaches_a_process_spawned_like_kd() -> None:
    # send_ctrl_break drops the caller's console, so it must not run in the pytest process.
    result = subprocess.run(
        [sys.executable, "-c", BREAK_PROBE],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    assert result.returncode == 0, result.stderr
    assert "GOT_BREAK" in result.stdout and "rc 3" in result.stdout


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
    service._kd_pipe_check = lambda pipe: False
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    assert exc.value.code == BACKEND_ERROR and "kd_setup_host" in exc.value.hint

    def dying(argv: list[str]) -> FakeKdProcess:
        proc = FakeKdProcess(argv)
        proc.inject(b"Cannot open pipe\r\n")
        proc.stop(1)
        return proc

    service.kd_sessions.clear()
    service._kd_pipe_check = lambda pipe: True
    service._kd_spawner = dying
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


def test_parse_bugcheck_reads_both_kd_formats() -> None:
    paren = parse_bugcheck(
        "*** Fatal System Error: 0x0000003b\n(0xc0000005,0xfffff800`0011,0x2,0x0)"
    )
    assert paren == {
        "code": "0x0000003b",
        "arguments": ["0xc0000005", "0xfffff8000011", "0x2", "0x0"],
    }
    kdnet = parse_bugcheck("Bugcheck code 0000007E\nArguments ffffffff`c0000005 00000000`00000000")
    assert kdnet["code"] == "0x0000007e"
    assert kdnet["arguments"][0] == "0xffffffffc0000005"
    assert parse_bugcheck("Breakpoint 0 hit") is None


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

    # kd_go still needs a prompt: on a running target it refuses with kd_not_broken.
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_go", {"vm": "win11-dev"})
    assert exc.value.code == KD_NOT_BROKEN

    # kd_exec, though, is not a dead end on a running target: it breaks in first, so the first
    # command after attach no longer fails with kd_not_broken.
    auto = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r"})
    assert "broke in first" in auto["note"]
    assert auto["state"] == "broken"
    assert service.runtime("win11-dev").guest_frozen

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
    # The command and its sentinel are separate lines now, so a line-eating meta command
    # cannot swallow the sentinel.
    assert "!process 0 0" in proc.commands
    assert any(c.startswith(".echo __NTDRIVE_END_") for c in proc.commands)

    state = await service.call("kd_state", {"vm": "win11-dev"})
    assert state["state"] == "broken" and state["last_event"]["event"] == "user_break"
    # last_event in a status view is summarized, not the whole break banner (context noise): the
    # kind and a one-line summary, with the full text left in kd_log_tail.
    assert "output" not in state["last_event"] and "summary" in state["last_event"]

    assert (await service.call("kd_go", {"vm": "win11-dev"}))["state"] == "running"
    proc.bugcheck()
    event = await service.call("kd_wait_event", {"vm": "win11-dev", "timeout": 5})
    assert event["event"] == "bugcheck"
    assert "Fatal System Error" in event["output"]
    # The bugcheck code and arguments ride the event, so no second command is needed to see them.
    assert event["bugcheck"]["code"] == "0x0000007e"
    assert event["bugcheck"]["arguments"][0] == "0xffffffffc0000005"

    tail = await service.call("kd_log_tail", {"vm": "win11-dev", "bytes": 4000})
    assert "Fatal System Error" in tail["text"]

    detached = await service.call("kd_detach", {"vm": "win11-dev"})
    assert detached["state"] == "detached"
    # Detaching from a live KDNET target is explained, so kd.exe transport chatter is not mistaken
    # for a guest crash.
    assert "guest keeps running" in detached["note"]
    # Breakpoints are cleared before the resume so a leftover int3 cannot spin the guest after kd
    # quits, then the target is resumed and kd told to quit.
    assert "bc *" in proc.commands
    assert proc.commands[-2:] == ["g", "q"]
    await settle()
    assert not service.runtime("win11-dev").guest_frozen


async def test_attach_probes_instead_of_waiting_for_a_banner(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    """A reconnected KDNET target is silent, so attach must ask, not wait out the timeout."""
    import ntdrive.kd.session as session_mod

    monkey = session_mod.CONNECTED_RE
    try:
        # Make the banner unmatchable: the target is there but never announces itself, which is
        # exactly what KDNET does after a reconnect.
        session_mod.CONNECTED_RE = re.compile(rb"THIS_NEVER_MATCHES")
        started = time.monotonic()
        attached = await service.call("kd_attach", {"vm": "win11-dev", "timeout": 120})
    finally:
        session_mod.CONNECTED_RE = monkey
    elapsed = time.monotonic() - started
    # It found the target by breaking in, and resumed it, so it is running and not frozen.
    assert attached["state"] == "running"
    assert "note" not in attached
    # The whole point: it returned in seconds, not after the 120 s timeout.
    assert elapsed < 30, f"attach waited {elapsed:.0f}s instead of probing"
    assert not service.runtime("win11-dev").guest_frozen
    # The probe's own break must not show up as an event: state running with a DbgBreakPoint
    # last_event was the confusing post-revert signal.
    assert attached["last_event"] is None
    assert (await service.call("kd_state", {"vm": "win11-dev"}))["last_event"] is None


async def test_kd_sample_collects_rows_and_clears_only_its_breakpoint(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    proc.break_on_go = True
    # A breakpoint the caller already had must survive the sample.
    await service.call("kd_exec", {"vm": "win11-dev", "cmd": "bp mod!Existing"})

    result = await service.call(
        "kd_sample",
        {
            "vm": "win11-dev",
            "symbol": "win32kfull!RFONTOBJ::vDeleteRFONT",
            "n": 3,
            "exprs": ["poi(@rcx)", "du poi(@rdx)"],
        },
    )
    assert result["hits"] == 3 and result["stopped_because"] == "n"
    assert [row["hit"] for row in result["rows"]] == [1, 2, 3]
    assert set(result["rows"][0]["values"]) == {"poi(@rcx)", "du poi(@rdx)"}
    # The breakpoint is plain: no condition is ever compiled into it (that is what NMIs a guest).
    bp_cmds = [c for c in proc.commands if c.startswith("bp ")]
    assert bp_cmds == ["bp mod!Existing", "bp win32kfull!RFONTOBJ::vDeleteRFONT"]
    assert not any(".if" in c or "gc" in c for c in proc.commands)
    # It cleared its own breakpoint and left the caller's alone.
    assert proc.bps == ["0"]


async def test_kd_sample_skips_hits_whose_condition_is_zero(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    proc.break_on_go = True
    proc.eval_value = 0  # the condition is false at every hit
    result = await service.call(
        "kd_sample",
        {
            "vm": "win11-dev",
            "symbol": "mod!Hot",
            "n": 2,
            "condition": "@rcx == 0x41",
            "max_seconds": 2,
        },
    )
    # The wall-clock cap ends it rather than looping forever on a hot symbol.
    assert result["hits"] == 0 and result["skipped_by_condition"] > 0
    assert result["stopped_because"] == "max_seconds"
    assert any(c.startswith("? ") for c in proc.commands)


async def test_kd_bugcheck_classifies_without_analyze(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    """.bugcheck is two lines; !analyze -v is 20k tokens of chkimg noise on a patched kernel."""
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    result = await service.call("kd_bugcheck", {"vm": "win11-dev"})
    assert result["bugcheck"]["code"] == "0x0000003b"
    # kd splits a 64-bit argument with a backtick, so the halves are joined into one value.
    assert result["bugcheck"]["arguments"][0] == "0x00000000c0000005"
    assert len(result["bugcheck"]["arguments"]) == 4
    assert "note" not in result  # a real bugcheck was parsed
    # It never runs the expensive extension.
    assert not any("analyze" in c for c in kd_procs[-1].commands)


async def test_kd_exec_flags_a_deferred_breakpoint_that_will_never_fire(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # The most expensive misdiagnosis this project has had: bp on a symbol whose module is not
    # mapped is accepted silently, stays deferred, never fires, and the caller concludes the code
    # path is not taken. Every bp is now checked with bl and a deferred one is called out.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    proc.bl_override = " 0 eu             0001 (0001) (cldflt!HsmpRpParseBuffer)\r\n"
    out = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "bp cldflt!HsmpRpParseBuffer"})
    assert out["breakpoints"] == [
        {
            "id": "0",
            "status": "eu",
            "deferred": True,
            "location": "0001 (0001) (cldflt!HsmpRpParseBuffer)",
        }
    ]
    assert "will NOT fire" in out["warning"] and "module is not mapped" in out["warning"]
    assert "sxe ld:" in out["warning"]  # the way to catch the module load is named
    # A bound breakpoint says so instead of warning, so "no warning" is not silence.
    proc.bl_override = " 0 e Disable Clear  fffff800`00001000  nt!NtCreateFile\r\n"
    bound = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "bp nt!NtCreateFile"})
    assert bound["breakpoints"][0]["deferred"] is False
    assert "warning" not in bound and "bound and will fire" in bound["note"]
    # A command that sets no breakpoint pays no bl round trip.
    proc.commands.clear()
    await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r rip"})
    assert "bl" not in proc.commands


async def test_kd_exec_refuses_commands_that_resume_the_target(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # `g` hands the target back to the CPU, so no prompt returns and kd_exec can only time out.
    # kd_go is the same thing with the bookkeeping, and the habit of typing g is easy to fall into.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    for resumer in ("g", "gc", " gh ", "gn 0x1000"):
        with pytest.raises(NtDriveError) as exc:
            await service.call("kd_exec", {"vm": "win11-dev", "cmd": resumer})
        assert exc.value.code == "invalid_args", resumer
        assert "kd_go" in exc.value.hint and "kd_wait_event" in exc.value.hint
    # Stepping commands do come back to the prompt, so they stay allowed, and a bp whose action
    # string merely contains 'gc' is a breakpoint, not a continue.
    for fine in ("p", "t", "gu", "bp nt!Foo \"j (@rcx=0) 'gc'; 'gc'\""):
        out = await service.call("kd_exec", {"vm": "win11-dev", "cmd": fine})
        assert out["outputs"], fine


async def test_kd_symcheck_tells_missing_types_from_missing_symbols(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # A cached PDB with function names but no type information fails dt, !pool and !process at
    # once, which reads as the commands being wrong. symcheck separates the two cases.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    proc.responses = {
        ".sympath": "Symbol search path is: srv*C:\\symbols*https://msdl.microsoft.com\r\n",
        "dt nt!_LIST_ENTRY": "   +0x000 Flink : Ptr64 _LIST_ENTRY\r\n",
        "x nt!KeBugCheckEx": "fffff800`00112233 nt!KeBugCheckEx (void)\r\n",
    }
    good = await service.call("kd_symcheck", {"vm": "win11-dev"})
    assert good["types_ok"] is True and good["names_ok"] is True
    assert "srv*C:\\symbols" in good["symbol_path"] and "can work" in good["note"]

    # Names resolve, types do not: the corrupt-cache case, and the cure is a FRESH cache dir.
    proc.responses["dt nt!_LIST_ENTRY"] = "Symbol nt!_LIST_ENTRY not found.\r\n"
    broken = await service.call("kd_symcheck", {"vm": "win11-dev"})
    assert broken["types_ok"] is False and broken["names_ok"] is True
    assert "NO type information" in broken["note"] and "symbols2" in broken["note"]

    # Nothing resolves: the PDB is missing, not incomplete.
    proc.responses["x nt!KeBugCheckEx"] = "Couldn't resolve error at 'nt!KeBugCheckEx'\r\n"
    gone = await service.call("kd_symcheck", {"vm": "win11-dev"})
    assert gone["names_ok"] is False and "missing rather than incomplete" in gone["note"]


async def test_kd_exec_names_a_symbol_error_as_a_bad_pdb(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # The banner reads like the command was wrong, so say it is the PDB and name the cure.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    kd_procs[-1].responses = {
        "!pool ffffd000": "Either you specified an unqualified symbol, or bad symbols\r\n"
    }
    out = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "!pool ffffd000"})
    assert "PDB is incomplete" in out["warning"] and "kd_symcheck" in out["warning"]


async def test_kd_exec_warns_about_a_breakpoint_that_resumes_itself(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # A conditional breakpoint that resumes the target itself costs a KDNET round trip per hit; on
    # a hot path that NMIs the guest with bugcheck 0x80 and has cost live sessions two snapshots.
    # The breakpoint is set by the time this returns, so the warning must ride the result the
    # caller reads before kd_go lets it fire.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    hot = "bp nt!NtCreateFile \"j (@rcx=0) 'gc'; 'gc'\""
    risky = await service.call("kd_exec", {"vm": "win11-dev", "cmd": hot})
    assert "bugcheck 0x80" in risky["warning"] and "kd_sample" in risky["warning"]
    assert "bc" in risky["warning"]
    # A plain breakpoint, and a listing, carry no warning: only self-resuming ones flood the link.
    # `g` is not in the list because kd_exec refuses it outright now (it never returns a prompt).
    for safe_cmd in ("bp nt!NtCreateFile", "bl", "bc *"):
        out = await service.call("kd_exec", {"vm": "win11-dev", "cmd": safe_cmd})
        assert "warning" not in out, safe_cmd


async def test_kd_capture_fault_automates_the_trap_and_stack(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # The manual OOB-write capture (cache sympath, switch processor, find the KTRAP_FRAME by the
    # fault RIP, .trap it, kb/lm) in one call.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    kd_procs[-1].bugcheck()
    assert (await service.call("kd_wait_event", {"vm": "win11-dev", "timeout": 5}))["event"] == (
        "bugcheck"
    )
    result = await service.call("kd_capture_fault", {"vm": "win11-dev", "processor": 1})
    assert result["mode"] == "trap"
    assert result["bugcheck"]["code"] == "0x0000003b"
    # Arg3 is the default fault RIP, and the trap frame is its stack match minus 0x168.
    assert result["fault_rip"] == "0xffffd000aabbccdd"
    assert result["trap_frame"] == "0xffffd0000a1b2a98"
    assert result["trap_candidates"] == ["0xffffd0000a1b2a98"]
    assert "output of [kb]" in result["stack"] and "output of [lm k]" in result["modules"]
    # A cache-only symbol path is set first so the symbol-heavy capture cannot wedge on the network.
    assert result["cache_symbols"] is True and result["symbol_path"] == r"cache*C:\symbols"
    cmds = kd_procs[-1].commands
    assert r".sympath cache*C:\symbols" in cmds and "~1s" in cmds
    assert "s -q @rsp L800 0xffffd000aabbccdd" in cmds
    assert "lm k" in cmds and any(c.startswith(".trap 0x") for c in cmds)


async def test_kd_capture_fault_can_use_a_context_record(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # A bugcheck that carries a CONTEXT pointer (0x3B / 0x7E Arg3) skips the stack search.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    kd_procs[-1].bugcheck()
    await service.call("kd_wait_event", {"vm": "win11-dev", "timeout": 5})
    result = await service.call(
        "kd_capture_fault",
        {"vm": "win11-dev", "context_record": "0xffffd00011112222", "cache_symbols": False},
    )
    assert result["mode"] == "context" and result["context_record"] == "0xffffd00011112222"
    cmds = kd_procs[-1].commands
    # The registers come out of CONTEXT with dq. .cxr would wedge kd over KDNET every time.
    assert "dq 0xffffd00011112222+0x78 L11" in cmds
    assert not any(c.startswith(".cxr") for c in cmds)
    assert not any(c.startswith("s -q") for c in cmds)  # no stack search
    assert not any(c.startswith(".sympath") for c in cmds)  # cache_symbols was off


async def test_kd_capture_fault_needs_a_broken_target(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # Right after attach the target is running, not at a bugcheck break, so it refuses clearly.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_capture_fault", {"vm": "win11-dev"})
    assert exc.value.code == KD_NOT_BROKEN and "kd_wait_event" in exc.value.hint


async def test_kd_exec_pins_the_processor_when_asked(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    """kd resets the processor at every break, so the caller must be able to say where to run."""
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r", "processor": 2})
    cmds = kd_procs[-1].commands
    assert "~2s" in cmds and cmds.index("~2s") < cmds.index("r")


async def test_kd_exec_says_when_a_command_printed_nothing(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    """`.reload /f mod.sys` returns empty; the caller must be able to tell that from a swallow."""
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    kd_procs[-1].bps.clear()
    result = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "bl"})
    assert result["outputs"][0]["printed_nothing"] is True
    loud = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r"})
    assert loud["outputs"][0]["printed_nothing"] is False


async def test_kd_setup_guest_writes_config(service: NtDriveService, fake_transport) -> None:  # type: ignore[no-untyped-def]
    await service.call("vm_start", {"vm": "win11-dev"})
    result = await service.call("kd_setup_guest", {"vm": "win11-dev", "port": 50005})
    assert result["needs_reboot"] and result["port"] == 50005
    assert "bcdedit /debug on" in fake_transport.exec_log
    assert any("hostip:192.168.126.1 port:50005 key:" in c for c in fake_transport.exec_log)
    assert result["adopted"] is False
    assert service.config.vms["win11-dev"].kdnet.port == 50005
    key = service.config.vms["win11-dev"].kdnet.key
    assert key and "." in key
    with open(service.config.path, encoding="utf-8") as fh:
        saved = fh.read()
    # The port and the generated key are persisted, but the key never appears in the audit log.
    assert "50005" in saved and key in saved
    audit = (service.log_dir / "audit.jsonl").read_text()
    assert key not in audit

    # A guest that already debugs to this host (setup-guest.cmd did it): the port and key are
    # read back and saved, nothing is rewritten, and no reboot is needed once debug is on.
    fake_transport.exec_log.clear()
    fake_transport.exec_responses["bcdedit /dbgsettings"] = (
        "debugtype               NET\nhostip                  192.168.126.1\n"
        "port                    50007\nkey                     ab12.cd34.ef56.7a8b\n"
        "dhcp                    Yes\nThe operation completed successfully.\n"
    )
    fake_transport.exec_responses["bcdedit /enum"] = (
        "Windows Boot Loader\n-------------------\nidentifier              {current}\n"
        "debug                   Yes\n"
    )
    result = await service.call("kd_setup_guest", {"vm": "win11-dev"})
    assert result["adopted"] is True and result["needs_reboot"] is False
    # {current} must be quoted, or PowerShell turns it into -encodedCommand and bcdedit fails.
    assert "bcdedit /enum '{current}'" in fake_transport.exec_log
    assert "bcdedit /enum {current}" not in fake_transport.exec_log
    assert result["port"] == 50007
    assert service.config.vms["win11-dev"].kdnet.key == "ab12.cd34.ef56.7a8b"
    assert not any("dbgsettings net" in c for c in fake_transport.exec_log)
    assert "ab12.cd34.ef56.7a8b" not in str(result["steps"])
    assert "ab12.cd34.ef56.7a8b" not in (service.log_dir / "audit.jsonl").read_text()

    # Debug still off in the guest: adopted, but /debug on runs and a reboot is due.
    fake_transport.exec_responses["bcdedit /enum"] = "debug                   No\n"
    result = await service.call("kd_setup_guest", {"vm": "win11-dev"})
    assert result["adopted"] is True and result["needs_reboot"] is True
    assert fake_transport.exec_log[-1] == "bcdedit /debug on"

    # Another host IP in the guest: the settings are rewritten for this host, reusing the key.
    fake_transport.exec_responses["bcdedit /dbgsettings"] = (
        "debugtype               NET\nhostip                  10.0.0.9\n"
        "port                    50007\nkey                     ab12.cd34.ef56.7a8b\n"
    )
    result = await service.call("kd_setup_guest", {"vm": "win11-dev"})
    assert result["adopted"] is False and result["needs_reboot"] is True
    assert any("hostip:192.168.126.1 port:50007 key:***" in c["cmd"] for c in result["steps"])
    # The guest's port is already used by another VM of this host: keep the guest's key,
    # write the next free port, reboot needed.
    from ntdrive.config import GuestConfig, KdnetConfig, VmConfig

    service.config.vms["other"] = VmConfig(
        name="other",
        vmx=service.config.vms["win11-dev"].vmx,
        kdnet_hostip="192.168.126.1",
        guest=GuestConfig(user="u"),
        kdnet=KdnetConfig(port=50007, key="9.9.9.9"),
    )
    fake_transport.exec_responses["bcdedit /dbgsettings"] = (
        "debugtype               NET\nhostip                  192.168.126.1\n"
        "port                    50007\nkey                     ab12.cd34.ef56.7a8b\n"
    )
    result = await service.call("kd_setup_guest", {"vm": "win11-dev"})
    assert result["adopted"] is False and result["needs_reboot"] is True
    assert result["port"] == 50008 and "50007" in result["note"]
    assert any("port:50008 key:***" in c["cmd"] for c in result["steps"])
    assert service.config.vms["win11-dev"].kdnet.key == "ab12.cd34.ef56.7a8b"
    del service.config.vms["other"]
    fake_transport.exec_responses.clear()

    # Serial: bcdedit serial settings, no key generated or saved.
    service.config.vms["win11-dev"].kd_transport = "serial"
    result = await service.call("kd_setup_guest", {"vm": "win11-dev"})
    assert result["transport"] == "serial" and result["port"] is None
    assert result["key_saved"] is False
    assert "serial debugport:1" in fake_transport.exec_log[-2]
    assert service.config.vms["win11-dev"].kdnet.key == "ab12.cd34.ef56.7a8b"  # untouched


async def test_kd_attach_reads_the_key_a_guest_script_set(
    service: NtDriveService, fake_transport, kd_procs: list[FakeKdProcess]
) -> None:  # type: ignore[no-untyped-def]
    # setup-host.cmd wrote the entry without a key, setup-guest.cmd configured KDNET in the guest:
    # the first attach reads the port and key back over SSH and saves them.
    cfg = service.config.vms["win11-dev"]
    cfg.kdnet.key = ""
    await service.call("vm_start", {"vm": "win11-dev"})
    fake_transport.exec_responses["bcdedit /dbgsettings"] = (
        "debugtype               NET\nhostip                  192.168.126.1\n"
        "port                    50011\nkey                     1a2b.3c4d.5e6f.7a8b\n"
    )
    fake_transport.exec_responses["bcdedit /enum"] = "debug                   Yes\n"
    attached = await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    assert attached["state"] == "running"
    assert cfg.kdnet.key == "1a2b.3c4d.5e6f.7a8b" and cfg.kdnet.port == 50011
    assert any("port=50011,key=1a2b.3c4d.5e6f.7a8b" in arg for arg in kd_procs[-1].argv)
    assert "1a2b.3c4d.5e6f.7a8b" not in (service.log_dir / "audit.jsonl").read_text()

    # A guest that has nothing configured yet: the settings are written and a reboot is asked for
    # instead of a bare "no key" error.
    await service.call("kd_detach", {"vm": "win11-dev"})
    cfg.kdnet.key = ""
    fake_transport.exec_responses.clear()
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    assert "needs a reboot" in exc.value.message and "vm_reboot" in exc.value.hint
    assert any("dbgsettings net hostip:192.168.126.1" in c for c in fake_transport.exec_log)
    assert cfg.kdnet.key  # saved, so the attach after the reboot needs no SSH


async def test_kd_exec_frames_a_line_eating_meta_command(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    # A dot-command eats to end of line. The sentinel is a separate line, so it survives and the
    # command's output still comes back framed.
    result = await service.call(
        "kd_exec", {"vm": "win11-dev", "cmd": r".sympath srv*c:\sym*https://x"}
    )
    out = result["outputs"][0]
    assert out["output"] == "output of [.sympath srv*c:\\sym*https://x]\nline two"
    proc = kd_procs[-1]
    assert ".sympath srv*c:\\sym*https://x" in proc.commands
    assert any(c.startswith(".echo __NTDRIVE_END_") for c in proc.commands)
    assert not any("; .echo" in c for c in proc.commands)


async def test_kd_exec_interrupts_a_wedged_command_and_stays_usable(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # A command that never returns (over KDNET `!process 0 0 <name>` is the classic) must not leave
    # the session dead: ntdrive breaks into kd.exe, the prompt comes back, and the error says the
    # next kd_exec works and names the network-sympath wedge so the caller can avoid it.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    kd_procs[-1].wedge_on = "!process 0 0 explorer.exe"
    with pytest.raises(NtDriveError) as exc:
        await service.call(
            "kd_exec", {"vm": "win11-dev", "cmd": "!process 0 0 explorer.exe", "timeout": 1}
        )
    assert exc.value.code == TIMEOUT
    assert exc.value.extra["interrupted"] is True and exc.value.extra["at_bugcheck"] is False
    assert "the next kd_exec works" in exc.value.hint
    assert "cache*" in exc.value.hint  # the sympath cure is offered
    # The session recovered to a prompt, so a plain command runs again.
    again = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r rip"})
    assert again["outputs"][0]["output"].startswith("output of [r rip]")


async def test_a_wedge_at_a_bugcheck_warns_against_detaching(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # At a bugcheck the target is halted in KeBugCheckEx: resuming completes the crash and reboots,
    # losing the context. So a wedge-interrupt there must not tell the caller to kd_detach (which
    # resumes), the way the non-bugcheck hint does.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    kd_procs[-1].bugcheck()  # the target is now broken at a bugcheck
    event = await service.call("kd_wait_event", {"vm": "win11-dev", "timeout": 5})
    assert event["event"] == "bugcheck"
    kd_procs[-1].wedge_on = "~1s"
    with pytest.raises(NtDriveError) as exc:
        await service.call(
            "kd_exec", {"vm": "win11-dev", "cmd": "dps rsp", "processor": 1, "timeout": 1}
        )
    assert exc.value.code == TIMEOUT and exc.value.extra["at_bugcheck"] is True
    assert "Do NOT kd_detach" in exc.value.hint
    assert "completes the crash" in exc.value.hint


async def test_kd_break_timeout_points_at_reconnecting_the_guest(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # A break that never reaches a prompt (the target is at [no_debuggee], never connected) tells
    # the caller to reboot the guest so it reconnects, instead of a bare timeout.
    service._kd_breaker = lambda proc: None
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    service.kd_sessions["win11-dev"].target_info = ""  # never saw the target connect
    with pytest.raises(NtDriveError) as exc:
        await service.call("kd_break", {"vm": "win11-dev", "timeout": 1})
    assert exc.value.code == TIMEOUT
    assert "no_debuggee" in exc.value.hint and "vm_reboot" in exc.value.hint


async def test_kd_state_is_authoritative_once_kd_dies(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    from ntdrive.core.state import KdState

    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    live = await service.call("kd_state", {"vm": "win11-dev"})
    assert live["attached"] is True and live["state"] == "broken"
    # kd.exe dies (a KDNET drop, a crash) and the reader thread has not reported it yet: the
    # tracked state still says broken. That once made an agent call kd_exec and get
    # kd_not_attached. The process is the truth, and the old events move out of the present.
    kd_procs[-1].stop(0)
    session = service.kd_sessions["win11-dev"]
    session.state = KdState.BROKEN
    dead = await service.call("kd_state", {"vm": "win11-dev"})
    assert dead["attached"] is False and dead["state"] == "detached"
    assert dead["target_info"] == "" and dead["last_event"] is None and dead["pid"] is None
    assert dead["previous_session"]["last_event"]["event"] == "user_break"
    assert "kd_attach" in dead["note"]


def test_normalize_symbol_path_makes_dbghelp_happy() -> None:
    from ntdrive.kd.session import normalize_symbol_path

    # The forward-slash cache is what symsrv rejected; the url keeps its slashes.
    assert (
        normalize_symbol_path("srv*C:/symbols*https://msdl.microsoft.com/download/symbols")
        == r"srv*C:\symbols*https://msdl.microsoft.com/download/symbols"
    )
    # A url with no store keyword gets srv*.
    assert normalize_symbol_path("C:/sym*https://x/y") == r"srv*C:\sym*https://x/y"
    # Already correct, and a plain local path, are left alone; empty stays empty.
    assert normalize_symbol_path(r"srv*C:\symbols*https://x") == r"srv*C:\symbols*https://x"
    assert normalize_symbol_path(r"C:\local\symbols") == r"C:\local\symbols"
    assert normalize_symbol_path("") == ""


async def test_kd_attach_passes_a_normalized_symbol_path(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    service.config.host.symbol_path = "srv*C:/symbols*https://msdl.microsoft.com/download/symbols"
    service.kd_sessions.clear()
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    argv = kd_procs[-1].argv
    assert "-y" in argv
    assert (
        argv[argv.index("-y") + 1] == r"srv*C:\symbols*https://msdl.microsoft.com/download/symbols"
    )


async def test_kd_exec_retries_a_short_read_once_after_a_wedge(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # A short memory read fails about a third of the time over KDNET on an identical call and the
    # very next one always works, so the drop swallowed it. One silent retry saves the caller a
    # round trip and a timeout that reads like a real failure.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    proc.wedge_on = "dq ffffd000 L4"
    proc.wedge_once = True  # like the live case: only the first attempt is swallowed
    out = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "dq ffffd000 L4", "timeout": 1})
    assert out["outputs"][0]["retried_after_wedge"] is True
    assert "output of [dq ffffd000 L4]" in out["outputs"][0]["output"]


async def test_kd_exec_does_not_retry_a_command_with_side_effects(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # Re-running a breakpoint, a reload or a symbol-heavy command would act twice or just double
    # the wait, so only pure short reads are retried.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    proc = kd_procs[-1]
    for unsafe in ("bp nt!NtCreateFile", ".reload /f nt", "dt nt!_EPROCESS", "dps @rsp L10"):
        proc.wedge_on = unsafe
        proc.wedge_once = True
        with pytest.raises(NtDriveError) as exc:
            await service.call("kd_exec", {"vm": "win11-dev", "cmd": unsafe, "timeout": 1})
        assert exc.value.code == TIMEOUT, unsafe
        assert exc.value.extra["interrupted"] is True, unsafe


async def test_kd_exec_warns_about_semicolon_batched_commands(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # A batched line wedges kd over KDNET, and an interrupt there can resume the target and lose a
    # bugcheck to AutoReboot. cmds is the safe equivalent, so the warning names it.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    out = await service.call("kd_exec", {"vm": "win11-dev", "cmd": "r rip; r rsp"})
    assert "semicolon-batched" in out["warning"] and "separate cmds entries" in out["warning"]
    # A semicolon inside a breakpoint's quoted action is not a batch.
    quoted = await service.call(
        "kd_exec", {"vm": "win11-dev", "cmd": "bp nt!Foo \"j (@rcx=0) '.echo a; .echo b'\""}
    )
    assert "semicolon-batched" not in str(quoted.get("warning", ""))


def test_decode_context_gprs_maps_the_x64_offsets() -> None:
    from ntdrive.core.tools.kd import decode_context_gprs

    # Two qwords per dq row, each row led by the address being dumped (which must not be read as a
    # value). 0x78 is Rax, so the order is Rax, Rcx, Rdx, Rbx, Rsp, ... and Rip last.
    dump = (
        "ffffd000`00000078  00000000`0000aaaa 00000000`0000cccc\n"
        "ffffd000`00000088  00000000`0000dddd 00000000`0000bbbb\n"
        "ffffd000`00000098  ffffd000`11112222 ffffd000`33334444\n"
    )
    regs = decode_context_gprs(dump)
    assert regs["rax"] == "0x000000000000aaaa"
    assert regs["rcx"] == "0x000000000000cccc"
    assert regs["rdx"] == "0x000000000000dddd"
    assert regs["rbx"] == "0x000000000000bbbb"
    assert regs["rsp"] == "0xffffd00011112222"
    assert regs["rbp"] == "0xffffd00033334444"
    # A short dump yields fewer registers rather than a wrong mapping.
    assert "rip" not in regs and len(regs) == 6


async def test_kd_capture_fault_reads_a_context_record_without_cxr(
    service: NtDriveService, kd_procs: list[FakeKdProcess]
) -> None:
    # .cxr on a bugcheck context record wedges kd over KDNET every time, so the GPRs are read out
    # of CONTEXT with one dq instead. The stack is then NOT the fault's, and the note says so.
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    kd_procs[-1].bugcheck()
    await service.call("kd_wait_event", {"vm": "win11-dev", "timeout": 5})
    proc = kd_procs[-1]
    proc.responses = {
        "dq 0xffffd00011112222+0x78 L11": (
            "ffffd000`00000078  00000000`0000aaaa 00000000`0000cccc\r\n"
            "ffffd000`00000088  00000000`0000dddd 00000000`0000bbbb\r\n"
            "ffffd000`00000098  ffffd000`cafe0000 ffffd000`33334444\r\n"
        )
    }
    out = await service.call(
        "kd_capture_fault",
        {"vm": "win11-dev", "context_record": "0xffffd00011112222", "cache_symbols": False},
    )
    assert out["mode"] == "context"
    assert out["registers"]["rax"] == "0x000000000000aaaa"
    assert out["registers"]["rsp"] == "0xffffd000cafe0000"
    # The raw stack is read from the fault's own rsp, with dq (dps would resolve a symbol per slot
    # and that is what wedges).
    assert "dq 0xffffd000cafe0000 L10" in proc.commands
    assert not any(c.startswith(".cxr") for c in proc.commands)
    assert "NOT unwound" in out["note"] and "stack_words" in out["note"]

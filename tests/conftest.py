"""Fakes for vmrun, kd.exe and the SSH channel, and fixtures that wire them into a service."""

from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from ntdrive.config import Config, GuestConfig, HostConfig, KdnetConfig, PolicyConfig, VmConfig
from ntdrive.core.service import NtDriveService
from ntdrive.errors import BACKEND_ERROR, NtDriveError
from ntdrive.hypervisor.vmware import VmwareAdapter
from ntdrive.kd.firewall import MANUAL_FIREWALL_HINT, FirewallStatus
from ntdrive.term.transport import CloseCallback, DataCallback, TermChannel, TermTransport

# -- fake vmrun ------------------------------------------------------------------------------


class FakeVmrun:
    """Simulates vmrun.exe. Keeps power state, snapshots and a call log."""

    def __init__(self, vmx: str) -> None:
        self.vmx = vmx
        self.running = False
        self.suspended = False
        self.snapshots: list[tuple[str, int]] = []  # (name, depth) in tree order
        self.calls: list[list[str]] = []
        self.fail_next: dict[str, int] = {}
        self.ip = "192.168.126.50"
        self.saw_vp = False
        self.vp_value = ""
        self.encrypted_live_snapshot_fails = False
        self.auth_fails = False  # when True, listSnapshots reports an encryption-auth failure
        # Number of calls right after a suspend that fail with the transient vmx error.
        self.config_unreadable_after_suspend = 0
        self.fail_start = False  # when True, `start` fails (resume after suspend cannot happen)
        self.reset_powers_off = False  # when True, `reset` leaves the VM off instead of resetting
        self.guest_files: dict[str, bytes] = {}  # what guest-tools copies put into the guest

    async def __call__(self, args: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(args))
        rest = args[1:]
        if not rest:
            return 255, "vmrun version 1.17.0 build-99999\nUsage: vmrun ..."
        if rest[:2] == ["-T", "ws"]:
            rest = rest[2:]
        self.saw_vp = False
        if rest[:1] == ["-vp"]:
            self.saw_vp = True
            self.vp_value = rest[1]
            rest = rest[2:]
        if rest[:1] == ["-gu"]:
            rest = rest[4:]
        cmd, rest = rest[0], rest[1:]
        if self.fail_next.get(cmd, 0) > 0:
            self.fail_next[cmd] -= 1
            return 255, "Error: The operation was temporarily unavailable"
        if cmd == "list":
            lines = [f"Total running VMs: {1 if self.running else 0}"]
            if self.running:
                lines.append(self.vmx)
            return 0, "\n".join(lines) + "\n"
        if self.suspended and self.config_unreadable_after_suspend > 0 and cmd != "start":
            self.config_unreadable_after_suspend -= 1
            return 4294967295, "Error: Cannot read the virtual machine configuration file"
        if cmd == "start":
            if self.fail_start:
                return 4294967295, "Error: The operation was temporarily unavailable"
            self.running, self.suspended = True, False
            Path(self.vmx).with_suffix(".vmss").unlink(missing_ok=True)
            return 0, ""
        if cmd == "stop":
            self.running = False
            return 0, ""
        if cmd == "reset":
            if self.reset_powers_off:
                # Seen live: `vmrun reset hard` can leave the VM off instead of resetting it.
                self.running = False
            return 0, ""
        if cmd == "suspend":
            self.running, self.suspended = False, True
            Path(self.vmx).with_suffix(".vmss").write_text("x")
            return 0, ""
        if cmd == "snapshot":
            # Emulate the encrypted-VM quirk: a live snapshot of a running VM is refused, but a
            # snapshot while suspended or off works. Toggle with encrypted_live_snapshot_fails.
            if self.encrypted_live_snapshot_fails and self.running and not self.suspended:
                return 4294967295, "Error: Authentication for encrypted virtual machine failed"
            self.snapshots.append((rest[1], 0))
            return 0, ""
        if cmd == "listSnapshots":
            if self.auth_fails:
                return 4294967295, "Error: Authentication for encrypted virtual machine failed"
            lines = [f"Total snapshots: {len(self.snapshots)}"]
            for name, depth in self.snapshots:
                lines.append("\t" * depth + name)
            return 0, "\n".join(lines) + "\n"
        if cmd == "revertToSnapshot":
            if rest[1] not in [n for n, _ in self.snapshots]:
                return 255, "Error: The snapshot does not exist"
            self.running = False
            return 0, ""
        if cmd == "deleteSnapshot":
            if rest[1] not in [n for n, _ in self.snapshots]:
                return 4294967295, "Error: The snapshot does not exist"
            # Same encrypted-VM quirk: deleting a memory snapshot in place is refused while running.
            if self.encrypted_live_snapshot_fails and self.running and not self.suspended:
                return 4294967295, "Error: Authentication for encrypted virtual machine failed"
            self.snapshots = [(n, d) for n, d in self.snapshots if n != rest[1]]
            return 0, ""
        if cmd == "clone":
            # rest = [source_vmx, dst_vmx, mode, -snapshot=..., -cloneName=...]. Make the dst file.
            dst = Path(rest[1])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text('displayName = "clone"\n', encoding="latin-1")
            return 0, ""
        if cmd == "deleteVM":
            Path(rest[0]).unlink(missing_ok=True)
            return 0, ""
        if cmd == "getGuestIPAddress":
            return 0, self.ip + "\n"
        if cmd == "captureScreen":
            Path(rest[1]).write_bytes(b"\x89PNG fake")
            return 0, ""
        if cmd == "copyFileFromHostToGuest":
            self.guest_files[rest[2]] = Path(rest[1]).read_bytes()
            return 0, ""
        if cmd == "copyFileFromGuestToHost":
            # vmrun writes the host-side file; the fake mirrors that so getsize() works.
            Path(rest[2]).parent.mkdir(parents=True, exist_ok=True)
            Path(rest[2]).write_bytes(self.guest_files.get(rest[1], b"pulled-by-guest-tools"))
            return 0, ""
        if cmd == "deleteFileInGuest":
            self.guest_files.pop(rest[1], None)
            return 0, ""
        if cmd == "runProgramInGuest":
            script = rest[-1]
            if "Remove-Item" in script and "Test-Path" in script:
                import re as _re2

                target = None
                mm = _re2.search(r"Remove-Item -LiteralPath '((?:[^']|'')*)'", script)
                if mm:
                    target = mm.group(1).replace("''", "'")
                report = _re2.search(
                    r"Set-Content -Encoding UTF8 -LiteralPath '((?:[^']|'')*)'", script
                )
                rp = report.group(1).replace("''", "'") if report else None
                existed = target in self.guest_files
                if existed:
                    self.guest_files.pop(target, None)
                if rp:
                    self.guest_files[rp] = (
                        b"ntdrive:deleted" if existed else b"ntdrive:absent"
                    ) + b"\r\n"
                return 0, ""
            if "Get-FileHash" in script:
                # The adapter's hash batch: quote-delimited paths, the last one is the report.
                import hashlib
                import re as _re

                paths = [p.replace("''", "'") for p in _re.findall(r"'((?:[^']|'')*)'", script)]
                targets, report = paths[:-1], paths[-1]
                lines = [
                    hashlib.sha256(self.guest_files[t]).hexdigest().upper() + " " + t
                    for t in targets
                    if t in self.guest_files
                ]
                self.guest_files[report] = ("\n".join(lines) + "\n").encode()
            return 0, ""
        return 255, f"Error: Unknown command {cmd}"


# -- fake terminal transport -----------------------------------------------------------------


class FakeChannel(TermChannel):
    """Echoes input and lets tests push output with emit()."""

    def __init__(
        self, on_data: DataCallback, on_close: CloseCallback, transport: FakeTransport
    ) -> None:
        self._on_data = on_data
        self._on_close = on_close
        self._open = True
        self.written: list[bytes] = []
        self.size = (0, 0)
        self._transport = transport

    def emit(self, data: bytes) -> None:
        self._on_data(data)

    def write(self, data: bytes) -> None:
        self.written.append(data)
        self._on_data(data.replace(b"\r", b"\r\n"))  # PTY echo
        if b"Clear-Host" in data:
            # Like a real shell: the setup line runs, the screen clears, a fresh prompt appears.
            self._on_data(b"\x1b[2J\x1b[H" + b"PS C:\\Users\\dev> ")
        self._transport.react(self, data)

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    def close(self) -> None:
        if self._open:
            self._open = False
            self._on_close()

    @property
    def is_open(self) -> bool:
        return self._open


class FakeTransport(TermTransport):
    """In-memory transport: channels, files and a canned exec_once."""

    name = "ssh"

    def __init__(self) -> None:
        self.channels: list[FakeChannel] = []
        self.files: dict[str, bytes] = {}
        self.exec_log: list[str] = []
        # (vm, account) for every transport the manager asked the factory for.
        self.opened_as: list[tuple[str, str]] = []
        # Canned exec_once output by command prefix, for tools that read the guest first.
        self.exec_responses: dict[str, str] = {}
        self.closed = False
        self.responder: Any = None
        self.fail_files = False  # when True, SFTP-style file ops raise like a dead SSH link
        # When set, file ops raise this instead of NtDriveError (a raw paramiko-style failure).
        self.raw_failure: BaseException | None = None
        self.fail_hash = False  # when True, only remote_sha256 fails

    async def open_channel(self, shell_cmd, cols, rows, on_data, on_close) -> TermChannel:  # type: ignore[no-untyped-def]
        chan = FakeChannel(on_data, on_close, self)
        self.channels.append(chan)
        chan.emit(b"PS C:\\Users\\dev> ")
        return chan

    def react(self, chan: FakeChannel, data: bytes) -> None:
        if self.responder is not None:
            self.responder(chan, data)

    async def close(self) -> None:
        self.closed = True

    def _maybe_fail(self, what: str) -> None:
        if self.raw_failure is not None:
            raise self.raw_failure
        if self.fail_files:
            from ntdrive.errors import BACKEND_ERROR, NtDriveError

            raise NtDriveError(BACKEND_ERROR, f"{what} failed: ssh link is dead")

    async def put_file(self, local: str, remote: str) -> int:
        self._maybe_fail("sftp put")
        data = Path(local).read_bytes()
        self.files[remote] = data
        return len(data)

    async def get_file(self, remote: str, local: str) -> int:
        self._maybe_fail("sftp get")
        if remote not in self.files:
            from ntdrive.errors import BACKEND_ERROR, NtDriveError

            raise NtDriveError(BACKEND_ERROR, "sftp get failed: no such file")
        data = self.files[remote]
        Path(local).write_bytes(data)
        return len(data)

    async def stat_file(self, remote: str):  # type: ignore[no-untyped-def]
        self._maybe_fail("sftp stat")
        if remote not in self.files:
            return None
        return {
            "size": len(self.files[remote]),
            "modified": "2026-01-01T00:00:00+00:00",
            "is_dir": False,
        }

    async def list_dir(self, remote: str):  # type: ignore[no-untyped-def]
        self._maybe_fail("sftp list")
        prefix = remote.replace("\\", "/").rstrip("/") + "/"
        entries = []
        for path, data in self.files.items():
            norm = path.replace("\\", "/")
            if norm.startswith(prefix) and "/" not in norm[len(prefix) :]:
                entries.append(
                    {
                        "name": norm[len(prefix) :],
                        "size": len(data),
                        "modified": "2026-01-01T00:00:00+00:00",
                        "is_dir": False,
                    }
                )
        return entries

    async def delete_file(self, remote: str, recurse: bool = False) -> None:
        self._maybe_fail("sftp delete")
        if remote not in self.files:
            raise FileNotFoundError(remote)
        del self.files[remote]

    async def remote_sha256(self, remote: str) -> str | None:
        import hashlib

        self._maybe_fail("sftp read")
        if self.fail_hash:
            from ntdrive.errors import BACKEND_ERROR, NtDriveError

            raise NtDriveError(BACKEND_ERROR, "sftp read failed: permission denied")
        return hashlib.sha256(self.files[remote]).hexdigest()

    async def exec_once(self, command: str, timeout: float = 60.0) -> tuple[int, str]:
        self.exec_log.append(command)
        for prefix, out in self.exec_responses.items():
            if command.startswith(prefix):
                return 0, out
        return 0, "The operation completed successfully.\n"


# -- fake kd.exe -----------------------------------------------------------------------------


class _FakeStdout:
    def __init__(self) -> None:
        self.q: queue.Queue[bytes | None] = queue.Queue()

    def read1(self, n: int) -> bytes:
        item = self.q.get()
        return b"" if item is None else item


class _FakeStdin:
    def __init__(self, proc: FakeKdProcess) -> None:
        self.proc = proc
        self.buf = b""

    def write(self, data: bytes) -> None:
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.proc.handle(line.decode())

    def flush(self) -> None:
        pass


class FakeKdProcess:
    """Behaves like kd.exe: connects, answers sentinel-framed commands, breaks on request."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.pid = 4242
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self)
        self._exit: int | None = None
        self.commands: list[str] = []
        self.broken = False
        self.break_on_go = False  # when True a `g` is followed by a breakpoint hit
        self.eval_value = 1  # what `? <expr>` evaluates to, for condition tests
        self.bps: list[str] = []
        self._next_bp = 0
        threading.Timer(
            0.05,
            self.inject,
            args=(b"Connected to Windows 11 26100 x64 target at (Wed Sep 10 2026), ptr64 TRUE\n",),
        ).start()

    def inject(self, data: bytes) -> None:
        self.stdout.q.put(data)

    def handle(self, line: str) -> None:
        self.commands.append(line)
        if line.strip() == "g":
            self.broken = False
            if self.break_on_go:
                threading.Timer(0.02, self.inject, args=(b"Breakpoint 0 hit\r\nkd> ",)).start()
            return
        if line.startswith("bp "):
            self.bps.append(str(self._next_bp))
            self._next_bp += 1
            self.inject(b"kd> ")  # bp itself prints nothing
            return
        if line.strip() == "bl":
            listing = "".join(
                f" {bp} e Disable Clear  fffff800`00001000  mod!Sym\r\n" for bp in self.bps
            )
            self.inject(listing.encode() + b"kd> ")
            return
        if line.startswith("bc "):
            for token in line[3:].split():
                if token in self.bps:
                    self.bps.remove(token)
            self.inject(b"kd> ")
            return
        if line.startswith("? "):
            self.inject(f"Evaluate expression: {self.eval_value} = 0x1\r\nkd> ".encode())
            return
        if line.strip() == "q":
            self.stop(0)
            return
        if line.strip() == ".reboot":
            self.broken = False
            threading.Timer(0.1, self.reconnect).start()
            return
        if line.startswith(".echo "):
            # The sentinel arrives on its own line now, framing the command written before it.
            self.inject(line[len(".echo ") :].encode() + b"\r\nkd> ")
            return
        if line.strip():
            self.inject(f"output of [{line}]\r\nline two\r\nkd> ".encode())
            return

    def reconnect(self) -> None:
        """What kd.exe prints when the rebooted target comes back."""
        self.inject(
            b"Shutdown occurred at (Wed Sep 10 2026)...unloading all symbol tables.\r\n"
            b"Waiting to reconnect...\r\n"
            b"Connected to Windows 11 26100 x64 target at (Wed Sep 10 2026), ptr64 TRUE\r\n"
        )

    def break_in(self) -> None:
        self.broken = True
        self.inject(
            b"Break instruction exception - code 80000003 (first chance)\r\nnt!DbgBreakPointWithStatus:\r\nkd> "
        )

    def bugcheck(self) -> None:
        self.broken = True
        self.inject(
            b"*** Fatal System Error: 0x0000007e\r\n"
            b"(0xffffffffc0000005,0xfffff80012345678,0x0,0x0)\r\n"
            b"A fatal system error has occurred.\r\nBugCheck 7E, {...}\r\nkd> "
        )

    def poll(self) -> int | None:
        return self._exit

    def stop(self, code: int) -> None:
        if self._exit is None:
            self._exit = code
            self.stdout.q.put(None)

    def terminate(self) -> None:
        self.stop(1)

    def kill(self) -> None:
        self.stop(9)


# -- fixtures --------------------------------------------------------------------------------


@pytest.fixture
def vmx_path(tmp_path: Path) -> str:
    vmx = tmp_path / "win11-dev" / "win11-dev.vmx"
    vmx.parent.mkdir()
    vmx.write_text('displayName = "win11-dev"\n')
    return str(vmx)


@pytest.fixture
def config(tmp_path: Path, vmx_path: str) -> Config:
    cfg = Config(
        host=HostConfig(
            vmrun=str(tmp_path / "vmrun.exe"),
            kd=str(tmp_path / "kd.exe"),
            kdnet=str(tmp_path / "kdnet.exe"),
            log_dir=str(tmp_path / "logs"),
            daemon_bind="127.0.0.1:18765",
        ),
        vms={
            "win11-dev": VmConfig(
                name="win11-dev",
                vmx=vmx_path,
                kdnet_hostip="192.168.126.1",
                kd_transport="net",  # most tests exercise KDNET, the serial ones say so
                guest=GuestConfig(user="dev", password="secret"),
                kdnet=KdnetConfig(port=50000, key="1.2.3.4"),
            )
        },
        path=str(tmp_path / "vms.yaml"),
    )
    import yaml

    (tmp_path / "vms.yaml").write_text(
        yaml.safe_dump(
            {"host": {}, "vms": {"win11-dev": {"vmx": vmx_path, "guest": {"user": "dev"}}}}
        )
    )
    return cfg


@pytest.fixture
def fake_vmrun(vmx_path: str) -> FakeVmrun:
    return FakeVmrun(vmx_path)


class FakeFirewall:
    """The host firewall as the tests see it.

    The test sets the status. The repair either fixes it or is refused, the way a cancelled
    UAC prompt refuses it. `unreadable` makes the check fail the way a missing PowerShell would.
    """

    def __init__(self) -> None:
        self.allow = True
        self.block_rules: list[str] = []
        self.refuse = False
        self.unreadable = ""
        self.checks = 0
        self.fixes = 0

    async def check(self, kd: str) -> FirewallStatus:
        self.checks += 1
        if self.unreadable:
            return FirewallStatus(checked=False, error=self.unreadable)
        return FirewallStatus(allow=self.allow, block_rules=list(self.block_rules))

    async def fix(self, kd: str, timeout: float) -> FirewallStatus:
        self.fixes += 1
        if self.refuse:
            raise NtDriveError(
                BACKEND_ERROR,
                "the UAC prompt was refused",
                MANUAL_FIREWALL_HINT,
            )
        self.allow, self.block_rules = True, []
        return await self.check(kd)


@pytest.fixture
def fake_firewall() -> FakeFirewall:
    return FakeFirewall()


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def kd_procs() -> list[FakeKdProcess]:
    return []


@pytest.fixture
def service(
    config: Config,
    fake_vmrun: FakeVmrun,
    fake_transport: FakeTransport,
    kd_procs: list[FakeKdProcess],
    fake_firewall: FakeFirewall,
) -> NtDriveService:
    def spawner(argv: list[str]) -> FakeKdProcess:
        proc = FakeKdProcess(argv)
        kd_procs.append(proc)
        return proc

    def breaker(proc: Any) -> None:
        proc.break_in()

    def transport_factory(vm: VmConfig, ip: str, account: str) -> FakeTransport:
        fake_transport.opened_as.append((vm.name, account))
        return fake_transport

    svc = NtDriveService(
        config,
        adapters={"vmware": VmwareAdapter(config.host.vmrun, runner=fake_vmrun)},
        policy=PolicyConfig(),
        transport_factory=transport_factory,
        kd_spawner=spawner,
        kd_breaker=breaker,
        kd_pipe_check=lambda pipe: True,
        firewall_check=fake_firewall.check,
        firewall_fix=fake_firewall.fix,
        coview_base="http://127.0.0.1:18765/coview?token=t",
    )
    svc.ssh_probe = always_reachable
    return svc


async def always_reachable(ip: str, port: int) -> bool:
    """Test probe: pretend the guest SSH port is open so the fake transport is used."""
    return True


async def never_reachable(ip: str, port: int) -> bool:
    """Test probe: the guest has no SSH, so file tools must use guest tools."""
    return False


async def settle(seconds: float = 0.05) -> None:
    """Let threads and callbacks drain."""
    await asyncio.sleep(seconds)

r"""KdSession: drive kd.exe over pipes.

Design:
- kd.exe is spawned with `-k net:port=P,key=K` (KDNET) or `-k com:pipe,port=\\.\pipe\...` (a
  VMware serial port) in its own hidden console and process group so that break-in can be
  delivered as CTRL_BREAK without touching the daemon's console.
- A reader thread pushes stdout into the event loop. State transitions are detected from the
  text: "Connected to" means the target is running, a trailing `kd>` prompt means broken in.
- Commands are framed with a sentinel: `<cmd>; .echo <sentinel>`. Everything printed between the
  write and the sentinel is that command's output, regardless of how noisy the target is.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Protocol

from ntdrive.core.state import KdState
from ntdrive.errors import (
    BACKEND_ERROR,
    KD_ALREADY_ATTACHED,
    KD_NOT_ATTACHED,
    KD_NOT_BROKEN,
    TIMEOUT,
    NtDriveError,
)

PROMPT_RE = re.compile(rb"(?:^|\r?\n)(?:\d+: )?kd> ?\Z")
CONNECTED_RE = re.compile(rb"Connected to (Windows[^\r\n]*)")
BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def generate_kdnet_key() -> str:
    """A 256-bit KDNET key: four base36 64-bit words separated by dots."""
    words: list[str] = []
    for _ in range(4):
        value = secrets.randbits(64)
        digits = ""
        while value:
            value, rem = divmod(value, 36)
            digits = BASE36[rem] + digits
        words.append(digits or "0")
    return ".".join(words)


def classify_break(text: str) -> str:
    """Name the event that brought the target to a prompt."""
    lowered = text.lower()
    if "bugcheck" in lowered or "fatal system error" in lowered:
        return "bugcheck"
    if "breakpoint" in lowered and "hit" in lowered:
        return "breakpoint"
    if "break instruction exception" in lowered:
        return "user_break"
    if "modload:" in lowered:
        return "module_load"
    if "assertion" in lowered:
        return "assertion"
    return "unknown"


class KdProcess(Protocol):
    """What KdSession needs from a process (real subprocess or a test fake)."""

    pid: int
    stdin: IO[bytes] | None
    stdout: IO[bytes] | None

    def poll(self) -> int | None:
        """Exit code or None while running."""
        ...

    def terminate(self) -> None:
        """Ask the process to stop."""
        ...

    def kill(self) -> None:
        """Force stop."""
        ...


Spawner = Callable[[list[str]], KdProcess]
Breaker = Callable[[KdProcess], None]
PipeCheck = Callable[[str], bool]


def named_pipe_exists(pipe: str) -> bool:
    r"""True when a host named pipe like \\.\pipe\ntdrive-win11 currently has a server.

    Listing \\.\pipe\ is the one safe check: opening or stat-ing the pipe would connect a client
    and steal the single instance VMware offers, so kd.exe could no longer attach.
    """
    if sys.platform != "win32":
        return True
    prefix = "\\\\.\\pipe\\"
    if not pipe.lower().startswith(prefix):
        return True
    name = pipe[len(prefix) :].lower()
    try:
        names = {entry.lower() for entry in os.listdir(prefix)}
    except OSError:
        return True
    return name in names


def spawn_kd(argv: list[str]) -> KdProcess:
    """Start kd.exe in a hidden console of its own with a separate process group."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startup
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    proc = subprocess.Popen(  # noqa: S603 - argv is built from config, not user text
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        **kwargs,
    )
    return proc


def send_ctrl_break(proc: KdProcess) -> None:
    """Deliver CTRL_BREAK to kd.exe by briefly attaching to its console."""
    if sys.platform != "win32":
        raise NtDriveError(BACKEND_ERROR, "break-in is only implemented on Windows")
    k32 = getattr(ctypes, "windll").kernel32  # noqa: B009 - not present off Windows
    k32.FreeConsole()
    if not k32.AttachConsole(proc.pid):
        code = ctypes.get_last_error()
        raise NtDriveError(BACKEND_ERROR, f"AttachConsole({proc.pid}) failed with {code}")
    try:
        k32.SetConsoleCtrlHandler(None, True)
        if not k32.GenerateConsoleCtrlEvent(1, proc.pid):  # 1 = CTRL_BREAK_EVENT
            raise NtDriveError(BACKEND_ERROR, "GenerateConsoleCtrlEvent failed")
    finally:
        k32.FreeConsole()
        k32.SetConsoleCtrlHandler(None, False)


class KdSession:
    """One kd.exe session for one VM."""

    def __init__(
        self,
        vm: str,
        kd_path: str,
        port: int,
        key: str,
        symbol_path: str,
        log_path: Path,
        loop: asyncio.AbstractEventLoop,
        spawner: Spawner | None = None,
        breaker: Breaker | None = None,
        buffer_capacity: int = 4 << 20,
        transport: str = "net",
        serial_pipe: str = "",
        pipe_check: PipeCheck | None = None,
    ) -> None:
        self.vm = vm
        self.kd_path = kd_path
        self.port = port
        self.key = key
        self.transport = transport
        self.serial_pipe = serial_pipe
        self.symbol_path = symbol_path
        self.log_path = log_path
        self._loop = loop
        self._spawn = spawner or spawn_kd
        self._break = breaker or send_ctrl_break
        self._pipe_check = pipe_check or named_pipe_exists
        self._capacity = buffer_capacity
        self._proc: KdProcess | None = None
        self._buf = bytearray()
        self._base = 0
        self.state = KdState.DETACHED
        self.target_info = ""
        self.last_event: dict[str, Any] | None = None
        self._changed = asyncio.Condition()
        self._reader: threading.Thread | None = None
        self._log: IO[bytes] | None = None
        self._lock = asyncio.Lock()

    # -- process ------------------------------------------------------------------------

    def argv(self) -> list[str]:
        """Command line for kd.exe, over KDNET or a VMware serial named pipe."""
        if self.transport == "serial":
            conn = f"com:pipe,port={self.serial_pipe},baud=115200,resets=0,reconnect"
        else:
            conn = f"net:port={self.port},key={self.key}"
        args = [self.kd_path, "-k", conn]
        if self.symbol_path:
            args += ["-y", self.symbol_path]
        return args

    async def attach(self, wait_for_target: bool = True, timeout: float = 120.0) -> dict[str, Any]:
        """Start kd.exe and optionally wait until the target connects."""
        if self._proc is not None and self._proc.poll() is None:
            raise NtDriveError(
                KD_ALREADY_ATTACHED, f"kd is already attached to {self.vm}", "call kd_detach first"
            )
        if self.transport == "serial" and not self._pipe_check(self.serial_pipe):
            raise NtDriveError(
                BACKEND_ERROR,
                f"serial pipe {self.serial_pipe} is not open on the host",
                "the VM must be running with the serial port in its vmx: run kd_setup_host "
                "with the VM off, then vm_start",
            )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("ab")
        # Reset the transcript buffer and last event so a re-attach on the same session object
        # (for example after a snapshot revert) does not see stale prompts or events.
        self._buf = bytearray()
        self._base = 0
        self.last_event = None
        self.target_info = ""
        try:
            self._proc = self._spawn(self.argv())
        except OSError as exc:
            raise NtDriveError(
                BACKEND_ERROR, f"cannot start kd.exe: {exc}", "check host.kd in vms.yaml"
            ) from exc
        proc = self._proc
        self.state = KdState.WAITING
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc,), name="kd-reader", daemon=True
        )
        self._reader.start()
        if self.transport == "serial":
            # Over a serial pipe kd does not announce "Connected to Windows" until it syncs, which
            # happens on the first break. So wait only long enough to notice a kd.exe that dies
            # at once, then treat the target as running; kd_break drives it to a prompt and the
            # pipe reconnects on its own thanks to `reconnect`.
            died = await self._wait_state({KdState.DETACHED}, min(timeout, 1.0), allow_timeout=True)
            if died or proc.poll() is not None:
                tail = bytes(self._buf[-2000:]).decode("utf-8", errors="replace")
                raise NtDriveError(
                    BACKEND_ERROR,
                    f"kd.exe exited right after start (rc={proc.poll()})",
                    "check host.kd and that no other debugger holds the serial pipe",
                    output=tail,
                )
            if self.state == KdState.WAITING:
                self.state = KdState.RUNNING
                await self._notify()
        elif wait_for_target:
            await self._wait_state({KdState.RUNNING, KdState.BROKEN}, timeout, allow_timeout=True)
        return self.status()

    def _read_loop(self, proc: KdProcess) -> None:
        assert proc.stdout is not None
        try:
            while True:
                data = (
                    proc.stdout.read1(65536)
                    if hasattr(proc.stdout, "read1")
                    else proc.stdout.read(1)
                )
                if not data:
                    break
                self._loop.call_soon_threadsafe(self._feed, data)
        except (OSError, ValueError):
            pass
        finally:
            self._loop.call_soon_threadsafe(self._process_exited, proc)

    def _feed(self, data: bytes) -> None:
        self._buf += data
        if self._log is not None:
            try:
                self._log.write(data)
                self._log.flush()
            except OSError:
                pass
        excess = len(self._buf) - self._capacity
        if excess > 0:
            del self._buf[:excess]
            self._base += excess
        m = CONNECTED_RE.search(data)
        if m:
            # KDNET prints this as soon as the target boots; a serial pipe prints it on the first
            # sync (usually the first break), so capture it whatever the state.
            self.target_info = m.group(1).decode("utf-8", errors="replace").strip()
            if self.state == KdState.WAITING:
                self.state = KdState.RUNNING
        if PROMPT_RE.search(self._buf[-64:]):
            if self.state in (KdState.RUNNING, KdState.WAITING):
                tail = self._buf[-4096:].decode("utf-8", errors="replace")
                kind = classify_break(tail)
                self.last_event = {
                    "event": kind,
                    "at": time.time(),
                    "output": tail[-2000:],
                }
            self.state = KdState.BROKEN
        self._loop.create_task(self._notify())

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    def _process_exited(self, proc: KdProcess | None = None) -> None:
        # Ignore the exit callback of a process we have already replaced (re-attach race).
        if proc is not None and proc is not self._proc:
            return
        self.state = KdState.DETACHED
        if self._log is not None:
            with contextlib.suppress(OSError):
                self._log.close()
            self._log = None
        self._loop.create_task(self._notify())

    @property
    def attached(self) -> bool:
        """True while kd.exe is alive."""
        return self._proc is not None and self._proc.poll() is None

    def _require_attached(self) -> KdProcess:
        if not self.attached:
            raise NtDriveError(
                KD_NOT_ATTACHED, f"kd is not attached to {self.vm}", "call kd_attach first"
            )
        assert self._proc is not None
        return self._proc

    def _write(self, text: str) -> None:
        proc = self._require_attached()
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.flush()
        if self._log is not None:
            with contextlib.suppress(OSError):
                self._log.write(text.encode("utf-8"))

    async def _wait_state(
        self, states: set[KdState], timeout: float, allow_timeout: bool = False
    ) -> bool:
        deadline = self._loop.time() + timeout
        while self.state not in states:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                if allow_timeout:
                    return False
                wanted = ", ".join(states)
                if self.transport == "serial":
                    hint = (
                        "check that the guest booted with serial debugging on (kd_setup_guest) "
                        "and that the vmx has the serial pipe (kd_setup_host)"
                    )
                else:
                    hint = (
                        "check the KDNET settings in the guest and the host firewall "
                        "(scripts/setup-host.ps1 as Administrator)"
                    )
                raise NtDriveError(
                    TIMEOUT,
                    f"kd did not reach {wanted} within {timeout:.0f}s (state={self.state})",
                    hint,
                )
            async with self._changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=min(remaining, 1.0))
        return True

    async def _wait_for_bytes(self, needle: bytes, start: int, timeout: float) -> int:
        """Wait until `needle` appears at or after absolute offset `start`; return its index."""
        deadline = self._loop.time() + timeout
        while True:
            rel = max(start - self._base, 0)
            idx = self._buf.find(needle, rel)
            if idx >= 0:
                return self._base + idx
            if not self.attached:
                raise NtDriveError(KD_NOT_ATTACHED, "kd.exe exited while waiting for output")
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise NtDriveError(
                    TIMEOUT,
                    "kd command did not finish in time",
                    "the target may be running; try kd_break",
                )
            async with self._changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=min(remaining, 1.0))

    # -- commands -----------------------------------------------------------------------

    def _require_broken(self) -> None:
        self._require_attached()
        if self.state != KdState.BROKEN:
            raise NtDriveError(
                KD_NOT_BROKEN,
                f"target is {self.state}; commands need a kd> prompt",
                "call kd_break (or kd_wait_event) first",
            )

    async def exec(
        self, cmds: list[str], timeout: float = 60.0, max_bytes: int = 65536
    ) -> list[dict[str, Any]]:
        """Run commands one after another and return each one's output."""
        self._require_broken()
        results: list[dict[str, Any]] = []
        async with self._lock:
            for cmd in cmds:
                sentinel = f"__NTDRIVE_END_{secrets.token_hex(4)}__"
                start = self._base + len(self._buf)
                started = time.monotonic()
                self._write(f"{cmd}; .echo {sentinel}\n")
                idx = await self._wait_for_bytes(sentinel.encode(), start, timeout)
                raw = bytes(self._buf[start - self._base : idx - self._base])
                text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
                text = re.sub(r"(?m)^(?:\d+: )?kd> ?$", "", text).strip("\n")
                truncated = len(text.encode("utf-8")) > max_bytes
                if truncated:
                    text = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
                results.append(
                    {
                        "cmd": cmd,
                        "output": text,
                        "truncated": truncated,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
                # Let the prompt come back before the next command.
                await self._wait_state({KdState.BROKEN}, timeout, allow_timeout=True)
        return results

    async def go(self) -> dict[str, Any]:
        """Resume the target."""
        self._require_broken()
        self._write("g\n")
        self.state = KdState.RUNNING
        await self._notify()
        return self.status()

    async def break_in(self, timeout: float = 10.0) -> dict[str, Any]:
        """Interrupt the running target and wait for the prompt."""
        proc = self._require_attached()
        if self.state == KdState.BROKEN:
            return self.status()
        start = self._base + len(self._buf)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._break, proc)
        await self._wait_state({KdState.BROKEN}, timeout)
        output = bytes(self._buf[start - self._base :]).decode("utf-8", errors="replace")
        self.last_event = {"event": "user_break", "at": time.time(), "output": output[-2000:]}
        result = self.status()
        result["output"] = output
        return result

    async def wait_event(self, timeout: float = 300.0) -> dict[str, Any]:
        """Wait until the running target stops at a prompt (bugcheck, breakpoint, ...)."""
        self._require_attached()
        if self.state == KdState.BROKEN:
            event = dict(self.last_event or {"event": "unknown", "output": ""})
            event["state"] = str(self.state)
            return event
        reached = await self._wait_state({KdState.BROKEN}, timeout, allow_timeout=True)
        if not reached:
            return {"event": "timeout", "output": "", "state": str(self.state)}
        event = dict(self.last_event or {"event": "unknown", "output": ""})
        event["state"] = str(self.state)
        return event

    async def detach(self, force: bool = False) -> dict[str, Any]:
        """Resume the target if it is broken, then stop kd.exe."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self.state = KdState.DETACHED
            return self.status()
        if self.state == KdState.BROKEN and not force:
            try:
                self._write("g\n")
                await asyncio.sleep(0.2)
            except (OSError, NtDriveError):
                pass
        with contextlib.suppress(OSError, NtDriveError):
            self._write("q\n")
        loop = asyncio.get_running_loop()
        for _ in range(20):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        if proc.poll() is None:
            await loop.run_in_executor(None, proc.terminate)
            await asyncio.sleep(0.2)
        if proc.poll() is None:
            await loop.run_in_executor(None, proc.kill)
        self._process_exited()
        return self.status()

    # -- introspection ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Wire form of the session state."""
        return {
            "state": str(self.state),
            "transport": self.transport,
            "port": self.port if self.transport == "net" else None,
            "serial_pipe": self.serial_pipe if self.transport == "serial" else None,
            "target_info": self.target_info,
            "last_event": self.last_event,
            "log_path": str(self.log_path),
            "pid": self._proc.pid if self._proc is not None else None,
        }

    def log_tail(self, nbytes: int = 16384) -> str:
        """Last bytes of the session transcript."""
        try:
            size = os.path.getsize(self.log_path)
            with self.log_path.open("rb") as fh:
                fh.seek(max(0, size - nbytes))
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return bytes(self._buf[-nbytes:]).decode("utf-8", errors="replace")

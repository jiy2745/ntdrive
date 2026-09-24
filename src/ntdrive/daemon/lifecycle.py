"""daemon.json, auto-start and shutdown of ntdrived.

Clients read %LOCALAPPDATA%/ntdrive/daemon.json (host, port, pid, token, version). When the
file is missing, the pid is dead or /health does not answer, the client starts a detached daemon
and waits for /health. The daemon stays up until `ntdrive daemon stop` (no idle exit).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil

from ntdrive import __version__
from ntdrive.config import find_config_path, state_dir
from ntdrive.errors import DAEMON_UNAVAILABLE, INVALID_ARGS, VERSION_MISMATCH, NtDriveError

log = logging.getLogger(__name__)

# A daemon still binding its port owns none for a moment, so only an older one counts as a leak.
_ORPHAN_MIN_AGE = 10.0


def orphaned_daemons(live_pid: int | None = None) -> list[int]:
    """Pids of ntdrived processes that own no listening port, so they serve nobody.

    A daemon that lost a port race used to linger instead of exiting (fixed in serve(), but existing
    ones stay until they are ended). A daemon that does listen somewhere is left alone, because a
    second config on another port is legitimate, and so is one younger than _ORPHAN_MIN_AGE.
    """
    orphans: list[int] = []
    now = time.time()
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            if "ntdrive.daemon.app" not in " ".join(proc.info["cmdline"] or []):
                continue
            if live_pid is not None and proc.info["pid"] == live_pid:
                continue
            if now - (proc.info["create_time"] or 0.0) < _ORPHAN_MIN_AGE:
                continue
            if not any(
                conn.status == psutil.CONN_LISTEN for conn in proc.net_connections(kind="tcp")
            ):
                orphans.append(int(proc.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return orphans


def reap_orphaned_daemons(live_pid: int | None = None) -> dict[str, list[int]]:
    """End the ntdrived processes that own no port. Never touches the live daemon."""
    ended: list[int] = []
    failed: list[int] = []
    for pid in orphaned_daemons(live_pid):
        try:
            proc = psutil.Process(pid)
            proc.kill()
            proc.wait(5)
            ended.append(pid)
        except psutil.NoSuchProcess:
            ended.append(pid)
        except (psutil.AccessDenied, psutil.TimeoutExpired, OSError) as exc:
            log.warning("could not reap ntdrived pid %s: %s", pid, exc)
            failed.append(pid)
    return {"reaped": ended, "failed": failed}


@dataclass
class DaemonInfo:
    """Contents of daemon.json."""

    host: str
    port: int
    pid: int
    token: str
    version: str
    started_at: float
    config_path: str = ""
    # Second secret, accepted only by the CoView session list and the terminal WebSocket. It is
    # the one that goes into CoView URLs, so tool results never carry the daemon token.
    view_token: str = ""

    @property
    def base_url(self) -> str:
        """http://host:port."""
        return f"http://{self.host}:{self.port}"

    @property
    def ws_base(self) -> str:
        """ws://host:port."""
        return f"ws://{self.host}:{self.port}"


def info_path() -> Path:
    """Where daemon.json lives."""
    return state_dir() / "daemon.json"


def read_info() -> DaemonInfo | None:
    """Parse daemon.json, or None when absent or corrupt."""
    path = info_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DaemonInfo(**data)
    except (OSError, ValueError, TypeError):
        return None


def write_info(info: DaemonInfo) -> None:
    """Write daemon.json, readable by this user only.

    On Windows LOCALAPPDATA already carries a per-user ACL. Elsewhere the mode is tightened
    because the file holds the bearer token.
    """
    path = info_path()
    path.write_text(json.dumps(asdict(info), indent=2), encoding="utf-8")
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def remove_info() -> None:
    """Delete daemon.json (called by the daemon on exit)."""
    with contextlib.suppress(OSError):
        info_path().unlink()


def new_token() -> str:
    """Random per-daemon bearer token."""
    return secrets.token_urlsafe(32)


def pid_alive(pid: int) -> bool:
    """True when a process with this pid exists and is not a zombie."""
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def probe_health(info: DaemonInfo, timeout: float = 2.0) -> dict[str, object] | None:
    """GET /health, or None when the daemon does not answer."""
    try:
        resp = httpx.get(f"{info.base_url}/health", timeout=timeout)
        if resp.status_code == 200:
            data: dict[str, object] = resp.json()
            return data
    except (httpx.HTTPError, ValueError):
        pass
    return None


def daemon_log_path() -> Path:
    """Where the detached daemon's stdout and stderr go (`ntdrive daemon logs` reads it)."""
    return state_dir() / "logs" / "daemon.out.log"


def _daemon_executable() -> str:
    """pythonw.exe next to the interpreter on Windows, else the interpreter itself.

    pythonw.exe is the GUI-subsystem Python: it never allocates a console, so the detached daemon
    cannot flash an empty window even for a frame. python.exe is a console program, and a hidden
    STARTUPINFO only hides the window conhost still briefly creates. The daemon writes to a log
    file, not a console, so it needs none.
    """
    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.is_file():
            return str(pyw)
    return sys.executable


def spawn_daemon(config_path: str | None = None) -> subprocess.Popen[bytes]:
    """Start ntdrived detached from this console, with no window of its own."""
    argv = [_daemon_executable(), "-m", "ntdrive.daemon.app"]
    if config_path:
        argv += ["--config", config_path]
    log = daemon_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    out = log.open("ab")
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # DETACHED_PROCESS gives the daemon no console, pythonw.exe makes sure it never wants one,
        # and the hidden STARTUPINFO is a last guard. Logs go to daemon.out.log, not a window.
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["startupinfo"] = startupinfo
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kwargs
    )


def resolve_config_path(explicit: str | None = None) -> str | None:
    """The vms.yaml this client would use, absolute, or None when there is none yet.

    Resolved on the client side (explicit path, NTDRIVE_CONFIG, then the state directory) and
    handed to the daemon, so the daemon never depends on the directory it was started from.
    """
    found = find_config_path(explicit)
    return str(found.resolve()) if found is not None else None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def ensure_daemon(
    config_path: str | None = None, autostart: bool = True, timeout: float = 25.0
) -> DaemonInfo:
    """Return a healthy daemon, starting one when allowed.

    A daemon that runs without any vms.yaml holds no sessions, so when a config exists now
    and autostart is allowed it is restarted on that file. A daemon that runs on a different
    file than this client resolved is left alone and reported, because it may hold live
    sessions: `ntdrive daemon restart` switches it deliberately.
    """
    wanted = resolve_config_path(config_path)
    info = read_info()
    if info is not None and pid_alive(info.pid):
        health = probe_health(info)
        if health is not None:
            _check_version(str(health.get("version", "")))
            if info.config_path and wanted and not _same_path(info.config_path, wanted):
                raise NtDriveError(
                    INVALID_ARGS,
                    f"the running daemon uses {info.config_path}, this client resolved {wanted}",
                    "run `ntdrive daemon restart` from this shell to switch the daemon, or drop "
                    "--config and NTDRIVE_CONFIG to use the daemon's file",
                )
            if info.config_path or wanted is None or not autostart:
                return info
            stop_daemon(info)
    if not autostart:
        raise NtDriveError(
            DAEMON_UNAVAILABLE,
            "ntdrived is not running",
            "run `ntdrive daemon start` or allow auto-start",
        )
    remove_info()
    spawn_daemon(wanted)
    deadline = time.time() + timeout
    while True:
        # Check before sleeping, so a daemon that is already answering costs nothing.
        info = read_info()
        if info is not None and probe_health(info) is not None:
            return info
        if time.time() >= deadline:
            break
        time.sleep(0.4)
    raise NtDriveError(
        DAEMON_UNAVAILABLE,
        f"ntdrived did not come up within {timeout:.0f}s",
        f"see {state_dir() / 'logs' / 'daemon.out.log'}",
    )


def restart_daemon(config_path: str | None = None, timeout: float = 25.0) -> DaemonInfo:
    """Stop the daemon if one runs, then start one on the config this client resolves."""
    info = read_info()
    if info is not None:
        stop_daemon(info)
    return ensure_daemon(config_path, autostart=True, timeout=timeout)


def _check_version(remote: str) -> None:
    if not remote:
        return
    if remote.split(".", maxsplit=1)[0] != __version__.split(".")[0]:
        raise NtDriveError(
            VERSION_MISMATCH,
            f"daemon version {remote} does not match client {__version__}",
            "run `ntdrive daemon restart`",
        )


def stop_daemon(info: DaemonInfo, timeout: float = 15.0) -> bool:
    """Ask the daemon to exit and wait for the pid to disappear."""
    with contextlib.suppress(httpx.HTTPError):
        httpx.post(
            f"{info.base_url}/api/shutdown",
            headers={"X-NtDrive-Token": info.token},
            timeout=5.0,
        )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(info.pid):
            remove_info()
            return True
        time.sleep(0.3)
    # The shutdown request did not end it in time, so kill it. A swallowed failure here is how a
    # daemon lingers with no port while a new one takes over, so it is logged rather than ignored.
    try:
        psutil.Process(info.pid).kill()
    except psutil.NoSuchProcess:
        pass
    except (psutil.AccessDenied, OSError) as exc:
        log.warning("could not kill ntdrived pid %s: %s", info.pid, exc)
    remove_info()
    return not pid_alive(info.pid)

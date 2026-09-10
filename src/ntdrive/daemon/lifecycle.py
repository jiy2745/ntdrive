"""daemon.json, auto-start and shutdown of ntdrived.

Clients read %LOCALAPPDATA%/ntdrive/daemon.json (host, port, pid, token, version). When the
file is missing, the pid is dead or /health does not answer, the client starts a detached daemon
and waits for /health. The daemon stays up until `ntdrive daemon stop` (no idle exit).
"""

from __future__ import annotations

import contextlib
import json
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
from ntdrive.config import state_dir
from ntdrive.errors import DAEMON_UNAVAILABLE, VERSION_MISMATCH, NtDriveError


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


def spawn_daemon(config_path: str | None = None) -> subprocess.Popen[bytes]:
    """Start ntdrived detached from this console."""
    argv = [sys.executable, "-m", "ntdrive.daemon.app"]
    if config_path:
        argv += ["--config", config_path]
    log = state_dir() / "logs" / "daemon.out.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    out = log.open("ab")
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(  # noqa: S603
        argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kwargs
    )


def ensure_daemon(
    config_path: str | None = None, autostart: bool = True, timeout: float = 25.0
) -> DaemonInfo:
    """Return a healthy daemon, starting one when allowed."""
    info = read_info()
    if info is not None and pid_alive(info.pid):
        health = probe_health(info)
        if health is not None:
            _check_version(str(health.get("version", "")))
            return info
    if not autostart:
        raise NtDriveError(
            DAEMON_UNAVAILABLE,
            "ntdrived is not running",
            "run `ntdrive daemon start` or allow auto-start",
        )
    remove_info()
    spawn_daemon(config_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        info = read_info()
        if info is not None and probe_health(info) is not None:
            return info
    raise NtDriveError(
        DAEMON_UNAVAILABLE,
        f"ntdrived did not come up within {timeout:.0f}s",
        f"see {state_dir() / 'logs' / 'daemon.out.log'}",
    )


def _check_version(remote: str) -> None:
    if not remote:
        return
    if remote.split(".")[0] != __version__.split(".")[0]:
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
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        psutil.Process(info.pid).kill()
    remove_info()
    return not pid_alive(info.pid)


def current_pid() -> int:
    """This process id (kept here so app.py has one import for lifecycle facts)."""
    return os.getpid()

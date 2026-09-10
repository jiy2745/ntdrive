"""SSH PTY transport on paramiko, plus SFTP for file transfer.

One SshPtyTransport per VM; every terminal session is a channel on that connection. Windows
OpenSSH allocates a ConPTY when a PTY is requested, so `exec_command("powershell.exe")` gives an
interactive shell regardless of the sshd default shell.

Every paramiko failure (SSHException, EOFError from a dropped link, socket errors) is converted
into NtDriveError so the file tools can fall back to guest tools instead of crashing the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import paramiko

from ntdrive.errors import BACKEND_ERROR, TIMEOUT, NtDriveError
from ntdrive.term.transport import CloseCallback, DataCallback, TermChannel, TermTransport

SHELL_COMMANDS: dict[str, str] = {
    "powershell": "powershell.exe -NoLogo",
    "pwsh": "pwsh.exe -NoLogo",
    "cmd": "cmd.exe /Q",
}


class HostKeyChanged(paramiko.SSHException):
    """The guest presented a different SSH host key than the one pinned for this VM."""


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Trust on first use, pinned per VM rather than per address.

    The guest IP comes from DHCP on the VMware NAT network and can move between VMs, so the key
    is stored under the VM name. The first connection records the key. A later connection with
    a different key is refused, which is what stops a machine that took over the address from
    receiving the guest password.
    """

    def __init__(self, store: Path) -> None:
        self.store = store

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        """Paramiko calls this for every host because no system known_hosts is loaded."""
        seen = {"type": key.get_name(), "key": key.get_base64()}
        try:
            stored = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        if stored is None:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps(seen, indent=2), encoding="utf-8")
            return
        if stored != seen:
            raise HostKeyChanged(
                f"host key for {hostname} changed ({stored.get('type')} -> {seen['type']})"
            )


async def probe_tcp_port(host: str, port: int, timeout: float = 2.0) -> bool:
    """One quick connect attempt. Closed or filtered ports return False fast."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


async def wait_for_port(host: str, port: int, timeout: float, interval: float = 2.0) -> bool:
    """Poll a TCP port until it accepts a connection or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await probe_tcp_port(host, port, timeout=3.0):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)


def _guarded[T](what: str, hint: str, fn: Callable[[], T]) -> T:
    """Run a blocking paramiko call and turn any failure into NtDriveError."""
    try:
        return fn()
    except NtDriveError:
        raise
    except Exception as exc:  # noqa: BLE001 - paramiko raises many unrelated types
        raise NtDriveError(BACKEND_ERROR, f"{what} failed: {exc}", hint) from exc


class SshChannel(TermChannel):
    """A paramiko channel with a reader thread."""

    def __init__(
        self, chan: paramiko.Channel, on_data: DataCallback, on_close: CloseCallback
    ) -> None:
        self._chan = chan
        self._on_data = on_data
        self._on_close = on_close
        self._open = True
        self._closed_once = threading.Event()
        self._thread = threading.Thread(target=self._reader, name="ssh-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        try:
            while True:
                data = self._chan.recv(65536)
                if not data:
                    break
                self._on_data(data)
        except Exception:  # noqa: BLE001 - any socket error means the channel is gone
            pass
        finally:
            self._open = False
            if not self._closed_once.is_set():
                self._closed_once.set()
                self._on_close()

    def write(self, data: bytes) -> None:
        """Send bytes; a dead channel raises."""
        if not self._open:
            raise NtDriveError(BACKEND_ERROR, "ssh channel is closed")
        try:
            self._chan.sendall(data)
        except Exception as exc:  # noqa: BLE001 - socket or paramiko error
            self._open = False
            raise NtDriveError(BACKEND_ERROR, f"ssh channel write failed: {exc}") from exc

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY."""
        with contextlib.suppress(Exception):
            self._chan.resize_pty(width=cols, height=rows)

    def close(self) -> None:
        """Close the channel."""
        self._open = False
        with contextlib.suppress(Exception):
            self._chan.close()

    @property
    def is_open(self) -> bool:
        """Whether the reader is still alive."""
        return self._open


class SshPtyTransport(TermTransport):
    """SSH connection to one guest."""

    name = "ssh"

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        connect_timeout: float = 10.0,
        host_key_file: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self.connect_timeout = connect_timeout
        # None (tests, ad hoc use) accepts any host key. The daemon always pins per VM.
        self.host_key_file = host_key_file
        self._client: paramiko.SSHClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> paramiko.SSHClient:
        async with self._lock:
            if self._client is not None:
                transport = self._client.get_transport()
                if transport is not None and transport.is_active():
                    return self._client
                self._client = None
            client = paramiko.SSHClient()
            if self.host_key_file is not None:
                client.set_missing_host_key_policy(PinnedHostKeyPolicy(self.host_key_file))
            else:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            loop = asyncio.get_running_loop()

            def _connect() -> None:
                try:
                    client.connect(
                        self.host,
                        port=self.port,
                        username=self.user,
                        password=self._password,
                        timeout=self.connect_timeout,
                        banner_timeout=self.connect_timeout,
                        auth_timeout=self.connect_timeout,
                        look_for_keys=False,
                        allow_agent=False,
                    )
                except HostKeyChanged as exc:
                    raise NtDriveError(
                        BACKEND_ERROR,
                        f"ssh connect to {self.host}:{self.port} refused: {exc}",
                        "the guest's SSH host key differs from the one recorded on first use. "
                        f"If the guest was reinstalled, delete {self.host_key_file} and retry. "
                        "Otherwise something else answers on that address.",
                    ) from exc

            await loop.run_in_executor(
                None,
                lambda: _guarded(
                    f"ssh connect to {self.host}:{self.port}",
                    "check that OpenSSH Server runs in the guest and the credentials are right",
                    _connect,
                ),
            )
            self._client = client
            return client

    async def open_channel(
        self,
        shell_cmd: str | None,
        cols: int,
        rows: int,
        on_data: DataCallback,
        on_close: CloseCallback,
    ) -> TermChannel:
        """Open a PTY channel; shell_cmd None means the sshd default shell."""
        client = await self._ensure()
        loop = asyncio.get_running_loop()

        def _open() -> paramiko.Channel:
            transport = client.get_transport()
            if transport is None:
                raise NtDriveError(BACKEND_ERROR, "ssh transport is gone")
            chan = transport.open_session(timeout=self.connect_timeout)
            chan.get_pty(term="xterm-256color", width=cols, height=rows)
            if shell_cmd:
                chan.exec_command(shell_cmd)
            else:
                chan.invoke_shell()
            chan.settimeout(None)
            return chan

        try:
            chan = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _guarded("opening the ssh channel", "", _open)),
                timeout=self.connect_timeout + 5,
            )
        except TimeoutError:
            raise NtDriveError(TIMEOUT, "opening the ssh channel timed out") from None
        return SshChannel(chan, on_data, on_close)

    async def close(self) -> None:
        """Close the SSH connection."""
        async with self._lock:
            if self._client is not None:
                with contextlib.suppress(Exception):
                    self._client.close()
                self._client = None

    async def _sftp(self) -> paramiko.SFTPClient:
        client = await self._ensure()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: _guarded(
                "opening sftp",
                "the guest sshd must have the sftp subsystem enabled (Win32-OpenSSH default)",
                client.open_sftp,
            ),
        )

    async def put_file(self, local: str, remote: str) -> int:
        """Upload one file with SFTP, creating the remote directory when needed."""
        sftp = await self._sftp()
        loop = asyncio.get_running_loop()
        target = _sftp_path(remote)

        def _put() -> int:
            try:
                _mkdirs(sftp, _remote_dirname(target))
                sftp.put(local, target)
                return os.path.getsize(local)
            finally:
                sftp.close()

        return await loop.run_in_executor(None, lambda: _guarded(f"sftp put {target}", "", _put))

    async def get_file(self, remote: str, local: str) -> int:
        """Download one file with SFTP."""
        sftp = await self._sftp()
        loop = asyncio.get_running_loop()
        source = _sftp_path(remote)

        def _get() -> int:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
                sftp.get(source, local)
                return os.path.getsize(local)
            finally:
                sftp.close()

        return await loop.run_in_executor(
            None,
            lambda: _guarded(
                f"sftp get {source}",
                "check the guest path; the guest-tools fallback runs next",
                _get,
            ),
        )

    async def remote_sha256(self, remote: str) -> str | None:
        """Hash a remote file by streaming it through SFTP."""
        sftp = await self._sftp()
        loop = asyncio.get_running_loop()
        source = _sftp_path(remote)

        def _hash() -> str:
            digest = hashlib.sha256()
            try:
                with sftp.open(source, "rb") as fh:
                    fh.prefetch()
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        digest.update(chunk)
            finally:
                sftp.close()
            return digest.hexdigest()

        return await loop.run_in_executor(None, lambda: _guarded(f"sftp read {source}", "", _hash))

    async def exec_once(self, command: str, timeout: float = 60.0) -> tuple[int, str]:
        """Run a non-interactive command (used by kd_setup_guest and the soft reboot)."""
        client = await self._ensure()
        loop = asyncio.get_running_loop()

        def _run() -> tuple[int, str]:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            return code, out + err

        return await loop.run_in_executor(
            None, lambda: _guarded(f"ssh exec of {command[:40]!r}", "", _run)
        )


def _sftp_path(path: str) -> str:
    r"""Windows sftp-server path form. `C:\a\b` becomes `/C:/a/b`.

    Win32-OpenSSH resolves a bare `C:/a/b` relative to the home directory, so a drive-letter
    path must start with a slash to be absolute.
    """
    cleaned = path.replace("\\", "/")
    if len(cleaned) >= 2 and cleaned[1] == ":" and cleaned[0].isalpha():
        return "/" + cleaned
    return cleaned


def _remote_dirname(path: str) -> str:
    cleaned = path.replace("\\", "/")
    return cleaned.rsplit("/", 1)[0] if "/" in cleaned else ""


def _mkdirs(sftp: paramiko.SFTPClient, directory: str) -> None:
    """Create a remote directory chain; ignores directories that already exist."""
    if not directory or directory.endswith(":"):
        return
    parts = directory.split("/")
    current = ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        if current.endswith(":"):
            continue
        try:
            sftp.stat(current)
        except OSError:
            with contextlib.suppress(OSError):
                sftp.mkdir(current)


__all__: list[str] = [
    "SHELL_COMMANDS",
    "HostKeyChanged",
    "PinnedHostKeyPolicy",
    "SshChannel",
    "SshPtyTransport",
    "probe_tcp_port",
    "wait_for_port",
]

"""Host path handling in the clients and paramiko error conversion in the SSH transport."""

import os
from typing import Any

import paramiko
import pytest

from ntdrive.errors import BACKEND_ERROR, NtDriveError
from ntdrive.paths import absolutize_local, is_absolute_local
from ntdrive.term.ssh import SshPtyTransport, _guarded


def test_absolutize_keeps_trailing_separator() -> None:
    out = absolutize_local("out/")
    assert os.path.isabs(out) and out.endswith(os.sep)
    out = absolutize_local("out\\")
    assert os.path.isabs(out) and out.endswith(os.sep)
    plain = absolutize_local("out")
    assert os.path.isabs(plain) and not plain.endswith(os.sep)
    assert is_absolute_local(plain) and not is_absolute_local("out")
    assert is_absolute_local("\\\\server\\share\\x")


def test_sdk_and_cli_absolutize_local() -> None:
    from ntdrive.core.registry import load_builtin_tools
    from ntdrive.sdk import NtDrive

    registry = load_builtin_tools()
    spec = registry.get("file_pull")
    assert spec is not None
    args = NtDrive._args(NtDrive.__new__(NtDrive), spec, ("vm", "C:\\a.txt"), {"local": "out/"})
    assert os.path.isabs(args["local"]) and args["local"].endswith(os.sep)


def test_guarded_converts_any_paramiko_failure() -> None:
    for raised in (paramiko.SSHException("boom"), EOFError(), OSError("socket"), ValueError("x")):

        def fail(exc: BaseException = raised) -> None:
            raise exc

        with pytest.raises(NtDriveError) as exc:
            _guarded("sftp put", "hint", fail)
        assert exc.value.code == BACKEND_ERROR and "sftp put failed" in exc.value.message
    # NtDriveError passes through untouched.
    inner = NtDriveError("timeout", "slow")
    with pytest.raises(NtDriveError) as exc:
        _guarded("x", "", lambda: (_ for _ in ()).throw(inner))
    assert exc.value is inner


class _DeadClient:
    """SSHClient stand-in whose sftp and exec calls fail like a dropped link."""

    def get_transport(self) -> Any:
        class T:
            def is_active(self) -> bool:
                return True

        return T()

    def open_sftp(self) -> Any:
        raise EOFError

    def exec_command(self, *a: Any, **k: Any) -> Any:
        raise paramiko.SSHException("SSH session not active")


async def test_transport_file_ops_raise_ntdrive_error_on_dead_link(tmp_path) -> None:  # type: ignore[no-untyped-def]
    transport = SshPtyTransport("127.0.0.1", 22, "u", "p")
    transport._client = _DeadClient()  # type: ignore[assignment]
    local = tmp_path / "f"
    local.write_bytes(b"x")
    for coro in (
        transport.put_file(str(local), "C:\\f"),
        transport.get_file("C:\\f", str(tmp_path / "g")),
        transport.remote_sha256("C:\\f"),
        transport.exec_once("hostname"),
    ):
        with pytest.raises(NtDriveError) as exc:
            await coro
        assert exc.value.code == BACKEND_ERROR

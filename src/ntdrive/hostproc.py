"""Run a host program without a console window and with a timeout.

The daemon runs detached, so any child started the normal way gets a console window of its own
and flashes it on the desktop. vmrun and the PowerShell helpers go through this runner. kd.exe
does not: break-in needs the console it gets from CREATE_NO_WINDOW, see ntdrive.kd.session.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from ntdrive.errors import TIMEOUT, NtDriveError


def no_window_kwargs(extra_flags: int = 0) -> dict[str, Any]:
    """Popen keyword arguments that keep a Windows child off the screen.

    CREATE_NO_WINDOW alone leaves a console (conhost) that can flash for a frame before it is
    hidden, which showed up as brief cmd-like windows during vm and kd operations. Pairing it
    with a STARTUPINFO that says SW_HIDE stops the window from ever being shown. The child still
    has a console, so kd.exe break-in over CTRL_BREAK keeps working. Empty off Windows.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW | extra_flags,
        "startupinfo": startupinfo,
    }


async def run_hidden(args: list[str], timeout: float) -> tuple[int, str]:
    """Run a command and return (exit code, combined stdout+stderr).

    stdin is closed so a child that asks a question fails instead of waiting forever. On timeout
    the child is killed and reaped, then NtDriveError(TIMEOUT) is raised.
    """
    kwargs: dict[str, Any] = no_window_kwargs()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kwargs,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise NtDriveError(
            TIMEOUT, f"{Path(args[0]).name} timed out after {timeout:.0f}s"
        ) from None
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def force_utf8_stdio() -> None:
    """Make stdout and stderr UTF-8 whatever the console code page says.

    A redirected stream (a pipe, a file) inherits the locale encoding, cp949 on a Korean Windows,
    and a kd output line or a symbol path with a character outside it raised UnicodeEncodeError
    and killed the CLI. The Windows console itself is Unicode already, so this changes only the
    redirected case, and it replaces rather than raises for anything still unencodable.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")

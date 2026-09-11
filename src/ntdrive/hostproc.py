"""Run a host program without a console window and with a timeout.

The daemon runs detached, so any child started the normal way gets a console window of its own
and flashes it on the desktop. vmrun and the PowerShell helpers go through this runner. kd.exe
does not: break-in needs the console it gets from CREATE_NO_WINDOW, see ntdrive.kd.session.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

from ntdrive.errors import TIMEOUT, NtDriveError


async def run_hidden(args: list[str], timeout: float) -> tuple[int, str]:
    """Run a command and return (exit code, combined stdout+stderr).

    stdin is closed so a child that asks a question fails instead of waiting forever. On timeout
    the child is killed and reaped, then NtDriveError(TIMEOUT) is raised.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
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

"""term_*: real-time terminal sessions."""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ntdrive.core.registry import tool
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import BACKEND_ERROR, INVALID_ARGS, SESSION_BUSY, TIMEOUT, NtDriveError
from ntdrive.term.keys import encode_key_list, encode_keys
from ntdrive.term.session import TermSession

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService


class OpenParams(VmParams):
    """term_open."""

    shell: Literal["powershell", "cmd", "pwsh"] | None = Field(
        default=None, description="Shell to start (default: guest.shell from vms.yaml)"
    )
    transport: Literal["auto", "ssh"] = Field(
        default="auto", description="auto or ssh (auto picks ssh)"
    )
    account: Literal["admin", "standard"] = Field(
        default="admin",
        description=(
            "admin (guest.user, the default) or standard (guest.standard_user, no administrator "
            "rights)"
        ),
    )
    cols: int = Field(default=120, ge=20, le=500, description="Terminal width in columns")
    rows: int = Field(default=40, ge=5, le=200, description="Terminal height in rows")
    boot_timeout: float = Field(
        default=60,
        ge=1,
        description=(
            "Seconds to wait for the guest to report an IP. Raise it for a guest that is still "
            "booting: the first boot after a bugcheck can run chkdsk for minutes"
        ),
    )


class SessionParams(BaseModel):
    """Tools that address one session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(description="Session id from term_open")


class SendParams(SessionParams):
    """term_send."""

    text: str = Field(default="", description="Text to type. {tokens} like {ctrl+c} are expanded")
    keys: list[str] = Field(default_factory=list, description="Burst of keys or text chunks")
    enter: bool = Field(default=True, description="Press Enter after the text")


class ReadParams(SessionParams):
    """term_read."""

    mode: Literal["delta", "screen"] = Field(
        default="delta",
        description="delta: output since the cursor. screen: the rendered screen a person sees",
    )
    until: str | None = Field(default=None, description="Regex to wait for (delta mode)")
    timeout: float = Field(default=0, ge=0, description="Seconds to wait when until is set")
    max_bytes: int = Field(
        default=65536, ge=256, le=1 << 20, description="Cap on the returned text (truncated says)"
    )
    cursor: int | None = Field(default=None, description="Absolute cursor (omit to continue)")
    clean: bool = Field(default=True, description="Strip terminal control sequences")


class ExecParams(SessionParams):
    """term_exec."""

    cmd: str = Field(description="Command to run in the shell")
    timeout: float = Field(default=60, ge=1, description="Seconds to wait for the command to end")
    max_bytes: int = Field(
        default=65536, ge=256, le=1 << 20, description="Cap on the returned text (truncated says)"
    )


class ResizeParams(SessionParams):
    """term_resize."""

    cols: int = Field(ge=20, le=500, description="New width in columns")
    rows: int = Field(ge=5, le=200, description="New height in rows")


class ListParams(BaseModel):
    """term_list."""

    model_config = ConfigDict(extra="forbid")

    vm: str | None = Field(default=None, description="Only sessions of this VM")
    include_closed: bool = Field(
        default=False,
        description=(
            "Also list closed and disconnected sessions. They pile up because every snap_revert "
            "and reboot replaces the open ones, so by default only usable sessions are listed and "
            "the rest are counted. term_prune forgets them for good"
        ),
    )
    limit: int = Field(default=50, ge=1, le=500, description="Cap on how many sessions are listed")


class PruneParams(BaseModel):
    """term_prune."""

    model_config = ConfigDict(extra="forbid")

    vm: str | None = Field(default=None, description="Only sessions of this VM")


def _session(service: NtDriveService, session_id: str) -> TermSession:
    session = service.term.get(session_id)
    service.ensure_not_frozen(session.vm)
    return session


def _frozen(service: NtDriveService, session: TermSession) -> Callable[[], bool]:
    """Abort predicate for waits: true once the debugger holds the guest at a kd> prompt."""
    return lambda: service.runtime(session.vm).guest_frozen


def _touch(service: NtDriveService, session: TermSession) -> None:
    info = service.state.term(session.session_id)
    if info is not None:
        info.last_activity = session.last_activity


@tool(
    "term_open",
    "Open a real-time PTY session (SSH) on the guest and return its session_id.",
    OpenParams,
    touches_guest=True,
    effect="additive",
)
async def term_open(service: NtDriveService, p: OpenParams) -> dict[str, Any]:
    """Open a session."""
    cfg = service.vm_cfg(p.vm)
    if p.account == "standard" and not cfg.guest.standard_user:
        raise NtDriveError(
            INVALID_ARGS,
            f"{p.vm} has no standard account (guest.standard_user is empty)",
            "in the guest run setup-guest.cmd (creates ntdrive-user), then ntdrive "
            "setup on the host and answer the standard account prompt, or use account=admin",
        )
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    ip = await service.guest_ip(cfg, timeout=p.boot_timeout)
    shell = p.shell or cfg.guest.shell
    session = await service.term.open(
        cfg, ip, shell, p.cols, p.rows, p.transport, account=p.account
    )
    info = service.state.term(session.session_id)
    service.state.record_event(p.vm, "term_open", session_id=session.session_id, account=p.account)
    return {
        "session_id": session.session_id,
        "vm": p.vm,
        "shell": shell,
        "transport": session.transport_name,
        "account": p.account,
        "coview_url": info.coview_url if info else "",
        "cols": p.cols,
        "rows": p.rows,
        # Probed once here, because at kill time the PTY is busy with the command that has to die.
        # null means term_kill cannot work on this session, nothing else is affected.
        "shell_pid": session.shell_pid,
    }


@tool(
    "term_send",
    "Type text and/or keys into a session and return at once. Tokens: {enter} {tab} {esc} "
    "{ctrl+c} {up}. Use it for a long-running command or one that will stop in the debugger, "
    "then term_read or kd_wait_event.",
    SendParams,
    positional=("session_id", "text"),
    touches_guest=True,
    effect="destructive",
)
async def term_send(service: NtDriveService, p: SendParams) -> dict[str, Any]:
    """Send input."""
    session = _session(service, p.session_id)
    data = encode_keys(p.text) if p.text else b""
    if p.keys:
        data += encode_key_list(p.keys)
    if p.enter and (p.text or not p.keys):
        data += b"\r"
    if not data:
        raise NtDriveError(INVALID_ARGS, "nothing to send", "give text or keys")
    sent = session.send(data, source="agent")
    _touch(service, session)
    result: dict[str, Any] = {
        "session_id": p.session_id,
        "bytes_sent": sent,
        "shell": session.shell,
        "state": session.state,
    }
    if "{ctrl+c}" in p.text.lower() or any("ctrl+c" in k.lower() for k in p.keys):
        # Ctrl+C is a console control event, not a kill: a program that handles or ignores it keeps
        # running, and it then owns this shell's input so every later term_exec queues behind it.
        # That desync once read as "the command is slow" for a whole session, so say it here.
        result["note"] = (
            "ctrl+c asks the foreground program to stop, it does not kill it. A program that "
            "ignores it keeps running and holds this shell, and later term_exec calls queue behind "
            "it: confirm with term_read, and if it is still alive call term_kill"
        )
    return result


@tool(
    "term_read",
    "Read new output (delta), wait for a regex (until), or render the screen (mode=screen). "
    "A wait ends with guest_frozen_by_debugger when the target stops at kd>.",
    ReadParams,
    positional=("session_id",),
    long_poll=True,
    effect="read",
)
async def term_read(service: NtDriveService, p: ReadParams) -> dict[str, Any]:
    """Read output."""
    session = _session(service, p.session_id)
    if p.mode == "screen":
        return {
            "session_id": p.session_id,
            "text": session.screen_text(),
            "cols": session.cols,
            "rows": session.rows,
            "cursor": session.ring.end,
            "state": session.state,
            "successor": session.successor,
        }
    if p.until:
        result = await session.wait_until(
            p.until,
            p.timeout,
            cursor=p.cursor,
            clean=p.clean,
            max_bytes=p.max_bytes,
            abort=_frozen(service, session),
        )
    else:
        result = session.read_delta(cursor=p.cursor, max_bytes=p.max_bytes, clean=p.clean)
    result["session_id"] = p.session_id
    return result


@tool(
    "term_exec",
    "Run one command in the session, wait for it to end (up to timeout) and return only its "
    "output and exit code. For a command that will stop in the kernel debugger or drop SSH, "
    "use term_send, then kd_wait_event or term_read.",
    ExecParams,
    positional=("session_id", "cmd"),
    touches_guest=True,
    long_poll=True,
    effect="destructive",
)
async def term_exec(service: NtDriveService, p: ExecParams) -> dict[str, Any]:
    """Marker-delimited one-shot command."""
    session = _session(service, p.session_id)
    if session.exec_in_flight:
        # Typing a second command now just queues it behind the first one in the shell's input, so
        # this call could only ever report a timeout while the output of the two interleaves.
        raise NtDriveError(
            SESSION_BUSY,
            f"session {p.session_id} is already running a term_exec",
            "wait for that call, or read the session with term_read. For a command started with "
            "term_send that will not end, send {ctrl+c}, and if the program ignores it call "
            "term_kill, which stops the shell's children and leaves the session open",
        )
    marker = f"__NTDRIVE_{secrets.token_hex(4)}__"
    # The PTY echoes what we type, so the typed line must not contain the marker: the shell
    # assembles it at run time (PowerShell concatenates, cmd drops the ^ escape). Seen live: a
    # PowerShell echo chunk that ended right after the marker matched the end-of-command regex
    # in 16 ms, and term_exec returned empty output with exit_code null for a shell that worked.
    if session.shell == "cmd":
        typed = f"__NT^{marker[4:]}"
        line = f"{p.cmd} & echo {typed} %ERRORLEVEL%\r"
    else:
        typed = f'"__NT" + "{marker[4:]}'
        # $LASTEXITCODE keeps the code of the last EXTERNAL program in the session, so a command
        # that runs none would report a stale number from something earlier (seen live: exit_code 1
        # for a cmdlet-only command that succeeded). Clearing it first makes the marker carry an
        # empty value, which the completion regex allows and which is reported as exit_code null
        # with the note that no external program ran.
        line = f'$global:LASTEXITCODE = $null; {p.cmd}; Write-Output ({typed} $LASTEXITCODE")\r'
    start_cursor = session.ring.end
    started = time.monotonic()
    session.exec_in_flight = True
    try:
        session.send(line.encode("utf-8"), source="agent")
        # The marker line ends the command: marker, a space, the exit code if the shell has one, end
        # of line. PowerShell prints no number before the first external program ran.
        result = await session.wait_until(
            rf"{re.escape(marker)} (-?\d*)[ \t]*\r?(?:\n|$)",
            p.timeout,
            cursor=start_cursor,
            clean=True,
            abort=_frozen(service, session),
        )
    finally:
        session.exec_in_flight = False
    text: str = result.get("text", "")
    if result.get("matched") is None:
        raise NtDriveError(
            TIMEOUT,
            f"command did not finish within {p.timeout:.0f}s",
            "read the session with term_read to see where it got to. If the command is still "
            "running send {ctrl+c}; a console program that ignores it keeps running in this shell "
            "and every later term_exec queues behind it, so call term_kill rather than retrying "
            "here",
            output=text[-p.max_bytes :],
            shell=session.shell,
        )
    # Only the shell's own marker line carries the marker in one piece.
    matches = list(re.finditer(rf"{re.escape(marker)} (-?\d+)", text))
    exit_code: int | None = None
    if matches:
        exit_code = int(matches[-1].group(1))
        body = text[: matches[-1].start()]
    else:
        anchor = re.search(rf"{re.escape(marker)}\b", text)
        body = text[: anchor.start()] if anchor else text
    # Everything up to and including the echo of our typed line is not the command's output:
    # the echo itself, and before it a fresh cmd session's banner or leftovers from an earlier
    # interactive command. The typed marker fragment identifies that line beyond doubt.
    lines = body.split("\n")
    echoed = [i for i, ln in enumerate(lines) if typed in ln]
    if echoed:
        lines = lines[echoed[-1] + 1 :]
    elif lines and p.cmd.strip() and p.cmd.strip()[:20] in lines[0]:
        lines = lines[1:]  # the echo wrapped across lines: at least drop its first line
    output = "\n".join(lines).strip("\n")
    truncated = len(output) > p.max_bytes
    _touch(service, session)
    result = {
        "session_id": p.session_id,
        # Always say which guest account ran the command. term_* runs as admin by default and
        # con_run in the interactive account, and a silent mismatch (a per-user resource created by
        # the wrong one) is hard to spot, so the account is never left implicit.
        "account": session.account,
        # Which shell parsed the command. A cmd line typed at PowerShell (where `&` is reserved)
        # fails with a parser error that reads like the command's own, so the shell is never left
        # implicit either.
        "shell": session.shell,
        "output": output[: p.max_bytes],
        "exit_code": exit_code,
        "state": session.state,
        "truncated": truncated,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }
    if exit_code is None:
        # The marker came back without a number. PowerShell sets $LASTEXITCODE only after an
        # external program ran, so a cmdlet-only command reports none. A dead session never
        # gets here: send and the wait raise session_disconnected.
        result["note"] = (
            "no numeric exit code came back (PowerShell sets $LASTEXITCODE only after an external "
            "program ran). state says whether the shell is still connected"
        )
    return result


class KillParams(SessionParams):
    """term_kill."""

    timeout: float = Field(
        default=30, ge=1, description="Seconds to wait for the kill to report back"
    )


# Each direct child of the shell is killed with its own tree (/T), so a run that spawned workers
# leaves no orphan, while the shell itself is never a target and the session stays open.
_KILL_SCRIPT = (
    "$kids = @(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | "
    "Select-Object ProcessId,Name); "
    "if ($kids.Count -eq 0) {{ 'NTDRIVE_NONE' }} else {{ foreach ($k in $kids) {{ "
    "taskkill /F /T /PID $k.ProcessId > $null 2>&1; "
    "'NTDRIVE_KILLED ' + $k.ProcessId + ' ' + $k.Name }} }}"
)


@tool(
    "term_kill",
    "Stop whatever the session's shell is running, keeping the session open. Use it when a "
    "command will not end and {ctrl+c} did not help: ctrl+c is a console control event that a "
    "program may ignore, and a program that ignores it keeps holding the shell so every later "
    "term_exec queues behind it. Each direct child of the shell is killed with its whole tree, so "
    "a run that spawned workers leaves no orphan. The shell itself is never killed. It runs over a "
    "separate SSH channel, so a busy PTY does not block it.",
    KillParams,
    positional=("session_id",),
    touches_guest=True,
    long_poll=True,
    effect="destructive",
)
async def term_kill(service: NtDriveService, p: KillParams) -> dict[str, Any]:
    """Kill the shell's children over a second channel, because the PTY itself is busy."""
    session = _session(service, p.session_id)
    if session.shell_pid is None:
        raise NtDriveError(
            BACKEND_ERROR,
            f"session {p.session_id} has no known shell PID, so its children cannot be found",
            "the PID is probed once when the session is opened and that probe did not answer. "
            "Open a fresh session with term_open and use that one, or kill the process by name "
            "from a second session (term_exec with taskkill /F /T /IM <exe>)",
        )
    cfg = service.vm_cfg(session.vm)
    transport = service.term.transport_for(cfg, session.account)  # type: ignore[arg-type]
    if transport is None or not hasattr(transport, "exec_once"):
        raise NtDriveError(
            BACKEND_ERROR,
            f"no live SSH transport for {session.vm} as {session.account}",
            "the guest may have rebooted: term_open a session again",
        )
    code, out = await transport.exec_once(
        _KILL_SCRIPT.format(pid=session.shell_pid), timeout=p.timeout
    )
    killed = [
        {"pid": int(m.group(1)), "name": m.group(2)}
        for m in re.finditer(r"NTDRIVE_KILLED (\d+) (\S+)", out)
    ]
    service.state.record_event(session.vm, "term_kill", session_id=p.session_id, killed=len(killed))
    result: dict[str, Any] = {
        "session_id": p.session_id,
        "vm": session.vm,
        "account": session.account,
        "shell": session.shell,
        "shell_pid": session.shell_pid,
        "killed": killed,
        "exit_code": code,
        "state": session.state,
    }
    if not killed:
        result["note"] = (
            "the shell had no child process, so nothing was killed: the command had already "
            "ended, or it runs somewhere other than this shell. term_read shows where the "
            "session stands, and con_run work runs in its own scheduled task, not here"
        )
    else:
        result["note"] = (
            "the shell is still open and at a prompt, so term_exec works again. Anything the "
            "killed run had half written is still half written: it was stopped, not undone"
        )
    return result


@tool(
    "term_resize",
    "Resize the PTY.",
    ResizeParams,
    positional=("session_id",),
    effect="additive",
    idempotent=True,
)
async def term_resize(service: NtDriveService, p: ResizeParams) -> dict[str, Any]:
    """Resize."""
    session = _session(service, p.session_id)
    session.resize(p.cols, p.rows)
    return {"session_id": p.session_id, "cols": p.cols, "rows": p.rows}


@tool(
    "term_close", "Close a session.", SessionParams, positional=("session_id",), effect="additive"
)
async def term_close(service: NtDriveService, p: SessionParams) -> dict[str, Any]:
    """Close."""
    await service.term.close(p.session_id)
    return {"session_id": p.session_id, "state": "closed"}


@tool(
    "term_list",
    "List the usable terminal sessions (their ids are in open, with each one's shell) and the "
    "CoView page that mirrors them live in a browser (#<session_id> selects one). Closed and "
    "disconnected sessions are only counted, not listed: they pile up as snap_revert and reboot "
    "replace the open ones, and dumping hundreds of them once cost a caller its whole token "
    "budget. include_closed=true lists them, term_prune forgets them.",
    ListParams,
    positional=("vm",),
    effect="read",
)
async def term_list(service: NtDriveService, p: ListParams) -> dict[str, Any]:
    """List, usable sessions first and the dead ones counted rather than dumped."""
    every = service.term.sessions(p.vm)
    counts: dict[str, int] = {}
    for info in every:
        state = str(info.get("state"))
        counts[state] = counts.get(state, 0) + 1
    listed = every if p.include_closed else [s for s in every if s.get("state") == "open"]
    shown = listed[: p.limit]
    result: dict[str, Any] = {
        "sessions": shown,
        "open": [s["session_id"] for s in every if s.get("state") == "open"],
        # Which shell each usable session runs, so a cmd line is not typed at PowerShell (`&` is
        # reserved there) or the other way round.
        "shells": {s["session_id"]: s.get("shell") for s in every if s.get("state") == "open"},
        "counts": counts,
        "total": len(every),
        "listed": len(shown),
        "truncated": len(shown) < len(listed),
        "coview": service.term.coview_base,
    }
    hidden = len(every) - len(listed)
    if hidden:
        result["note"] = (
            f"{hidden} closed or disconnected session(s) are counted in counts but not listed. "
            "term_prune forgets them, include_closed=true lists them"
        )
    return result


@tool(
    "term_prune",
    "Forget closed and disconnected terminal sessions (their open successors stay), so the "
    "list shows only what is usable.",
    PruneParams,
    positional=("vm",),
    effect="additive",
    idempotent=True,
)
async def term_prune(service: NtDriveService, p: PruneParams) -> dict[str, Any]:
    """Drop stale bookkeeping."""
    pruned = service.term.prune(p.vm)
    return {"pruned": pruned, "remaining": len(service.term.sessions(p.vm))}

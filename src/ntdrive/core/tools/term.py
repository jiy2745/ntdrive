"""term_*: real-time terminal sessions."""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import INVALID_ARGS, TIMEOUT, NtDriveError
from ntdrive.term.keys import encode_key_list, encode_keys
from ntdrive.term.session import TermSession


class OpenParams(VmParams):
    """term_open."""

    shell: Literal["powershell", "cmd", "pwsh"] | None = Field(
        default=None, description="Shell to start; defaults to guest.shell from vms.yaml"
    )
    transport: Literal["auto", "ssh"] = Field(default="auto", description="Transport")
    account: Literal["admin", "standard"] = Field(
        default="admin",
        description=(
            "Guest account to log in as: admin (guest.user, the administrator, the default) or "
            "standard (guest.standard_user, a plain user without administrator rights)"
        ),
    )
    cols: int = Field(default=120, ge=20, le=500)
    rows: int = Field(default=40, ge=5, le=200)


class SessionParams(BaseModel):
    """Tools that address one session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(description="Session id from term_open")


class SendParams(SessionParams):
    """term_send."""

    text: str = Field(default="", description="Text to type; {tokens} like {ctrl+c} are expanded")
    keys: list[str] = Field(default_factory=list, description="Burst of keys or text chunks")
    enter: bool = Field(default=True, description="Press Enter after the text")


class ReadParams(SessionParams):
    """term_read."""

    mode: Literal["delta", "screen"] = Field(default="delta")
    until: str | None = Field(default=None, description="Regex to wait for (delta mode)")
    timeout: float = Field(default=0, ge=0, description="Seconds to wait when until is set")
    max_bytes: int = Field(default=65536, ge=256, le=1 << 20)
    cursor: int | None = Field(default=None, description="Absolute cursor; omit to continue")
    clean: bool = Field(default=True, description="Strip terminal control sequences")


class ExecParams(SessionParams):
    """term_exec."""

    cmd: str = Field(description="Command to run in the shell")
    timeout: float = Field(default=60, ge=1)
    max_bytes: int = Field(default=65536, ge=256, le=1 << 20)


class ResizeParams(SessionParams):
    """term_resize."""

    cols: int = Field(ge=20, le=500)
    rows: int = Field(ge=5, le=200)


class ListParams(BaseModel):
    """term_list."""

    model_config = ConfigDict(extra="forbid")

    vm: str | None = Field(default=None, description="Only sessions of this VM")


def _session(service: NtDriveService, session_id: str) -> TermSession:
    session = service.term.get(session_id)
    service.ensure_not_frozen(session.vm)
    return session


def _touch(service: NtDriveService, session: TermSession) -> None:
    info = service.state.term(session.session_id)
    if info is not None:
        info.last_activity = session.last_activity


@tool(
    "term_open",
    "Open a real-time PTY session (SSH) on the guest, as the administrator or as a standard "
    "user, and return its session_id.",
    OpenParams,
    touches_guest=True,
)
async def term_open(service: NtDriveService, p: OpenParams) -> dict[str, Any]:
    """Open a session."""
    cfg = service.vm_cfg(p.vm)
    if p.account == "standard" and not cfg.guest.standard_user:
        raise NtDriveError(
            INVALID_ARGS,
            f"{p.vm} has no standard account (guest.standard_user is empty)",
            "in the guest run setup-guest.cmd -Standard (creates ntdrive-user), then ntdrive "
            "setup on the host and answer the standard account prompt; or use account=admin",
        )
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    ip = await service.guest_ip(cfg)
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
    }


@tool(
    "term_send",
    "Type text and/or a burst of keys into a session. Tokens: {enter} {tab} {esc} {ctrl+c} {up}.",
    SendParams,
    positional=("session_id", "text"),
    touches_guest=True,
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
    return {"session_id": p.session_id, "bytes_sent": sent}


@tool(
    "term_read",
    "Read new output (delta), wait for a regex (until), or render the screen (mode=screen).",
    ReadParams,
    positional=("session_id",),
    long_poll=True,
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
        result = await session.wait_until(p.until, p.timeout, cursor=p.cursor, clean=p.clean)
    else:
        result = session.read_delta(cursor=p.cursor, max_bytes=p.max_bytes, clean=p.clean)
    result["session_id"] = p.session_id
    return result


@tool(
    "term_exec",
    "Run one command in the session and return only its output and exit code.",
    ExecParams,
    positional=("session_id", "cmd"),
    touches_guest=True,
    long_poll=True,
)
async def term_exec(service: NtDriveService, p: ExecParams) -> dict[str, Any]:
    """Marker-delimited one-shot command."""
    session = _session(service, p.session_id)
    marker = f"__NTDRIVE_{secrets.token_hex(4)}__"
    if session.shell == "cmd":
        line = f"{p.cmd} & echo {marker} %ERRORLEVEL%\r"
    else:
        line = f'{p.cmd}; Write-Output "{marker} $LASTEXITCODE"\r'
    start_cursor = session.ring.end
    started = time.monotonic()
    session.send(line.encode("utf-8"), source="agent")
    result = await session.wait_until(
        rf"{marker} (-?\d*)\s*$", p.timeout, cursor=start_cursor, clean=True
    )
    text: str = result.get("text", "")
    if result.get("matched") is None:
        raise NtDriveError(
            TIMEOUT,
            f"command did not finish within {p.timeout:.0f}s",
            "read the session with term_read or send {ctrl+c}",
            output=text[-p.max_bytes :],
        )
    # The PTY echoes the command we typed, so `{marker} $LASTEXITCODE` appears literally before
    # the real `{marker} <code>` line. Match the marker followed by digits and take the last one.
    matches = list(re.finditer(rf"{re.escape(marker)} (-?\d+)", text))
    exit_code: int | None = None
    if matches:
        exit_code = int(matches[-1].group(1))
        body = text[: matches[-1].start()]
    else:
        anchor = re.search(rf"{re.escape(marker)}\b", text)
        body = text[: anchor.start()] if anchor else text
    # Drop the echoed command line (the PTY echoes what we typed).
    lines = body.split("\n")
    if lines and p.cmd.strip() and p.cmd.strip()[:20] in lines[0]:
        lines = lines[1:]
    output = "\n".join(lines).strip("\n")
    truncated = len(output) > p.max_bytes
    _touch(service, session)
    return {
        "session_id": p.session_id,
        "output": output[: p.max_bytes],
        "exit_code": exit_code,
        "truncated": truncated,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }


@tool("term_resize", "Resize the PTY.", ResizeParams, positional=("session_id",))
async def term_resize(service: NtDriveService, p: ResizeParams) -> dict[str, Any]:
    """Resize."""
    session = _session(service, p.session_id)
    session.resize(p.cols, p.rows)
    return {"session_id": p.session_id, "cols": p.cols, "rows": p.rows}


@tool("term_close", "Close a session.", SessionParams, positional=("session_id",))
async def term_close(service: NtDriveService, p: SessionParams) -> dict[str, Any]:
    """Close."""
    await service.term.close(p.session_id)
    return {"session_id": p.session_id, "state": "closed"}


@tool("term_list", "List terminal sessions and their state.", ListParams, positional=())
async def term_list(service: NtDriveService, p: ListParams) -> dict[str, Any]:
    """List."""
    return {"sessions": service.term.sessions(p.vm)}

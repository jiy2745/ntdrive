import asyncio
import re

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import (
    GUEST_FROZEN_BY_DEBUGGER,
    INVALID_ARGS,
    SESSION_DISCONNECTED,
    NtDriveError,
)
from ntdrive.term.keys import encode_key_list, encode_keys
from ntdrive.term.session import LOOKBACK, RingBuffer, TermSession, clean_text

from .conftest import FakeChannel, FakeTransport, settle


def test_key_tokens() -> None:
    assert encode_keys("{ctrl+c}") == b"\x03"
    assert encode_keys("ls{enter}") == b"ls\r"
    assert encode_keys("{up}{unknown}") == b"\x1b[A{unknown}"
    assert encode_key_list(["a", "{tab}", "{ctrl+z}"]) == b"a\t\x1a"


def test_ring_buffer_offsets() -> None:
    ring = RingBuffer(8)
    ring.append(b"0123456789")
    assert ring.base == 2 and ring.end == 10
    chunk, cursor, lost, truncated = ring.read(0, 4)
    assert chunk == b"2345" and cursor == 6 and lost and truncated
    chunk, cursor, lost, truncated = ring.read(6, 100)
    assert chunk == b"6789" and cursor == 10 and not lost and not truncated


def test_clean_text_strips_ansi() -> None:
    assert clean_text(b"\x1b[32mok\x1b[0m\r\nnext\x1b]0;title\x07") == "ok\nnext"


async def test_session_read_screen_and_wait() -> None:
    loop = asyncio.get_running_loop()
    session = TermSession("t-1", "vm", "powershell", "ssh", 40, 5, None, loop)
    chan = FakeChannel(session.on_data_threadsafe, session.on_close_threadsafe, FakeTransport())
    session.attach(chan)
    chan.emit(b"PS C:\\> ")
    await settle()
    first = session.read_delta()
    assert first["text"] == "PS C:\\> "
    assert session.read_delta()["text"] == ""
    waiter = asyncio.create_task(session.wait_until(r"done \d+", timeout=2))
    await settle()
    chan.emit(b"\x1b[1mworking\x1b[0m\r\ndone 42\r\nPS C:\\> ")
    result = await waiter
    assert result["matched"] == "done 42"
    assert "working" in result["text"]
    assert session.screen_text().splitlines()[0].startswith("PS C:\\> ")
    assert "done 42" in session.screen_text()
    session.send(b"dir\r")
    assert chan.written == [b"dir\r"]
    chan.close()
    await settle()
    assert session.state == "disconnected"
    with pytest.raises(NtDriveError) as exc:
        session.send(b"x")
    assert exc.value.code == SESSION_DISCONNECTED


def _marker(data: bytes) -> bytes:
    """The marker the shell will print, from the typed line that deliberately splits it."""
    m = re.search(rb'"__NT" \+ "(DRIVE_[0-9a-f]+__)', data)
    assert m, data
    return b"__NT" + m.group(1)


async def test_term_tools_end_to_end(
    service: NtDriveService, fake_transport: FakeTransport
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    opened = await service.call("term_open", {"vm": "win11-dev"})
    sid = opened["session_id"]
    assert opened["transport"] == "ssh"
    assert opened["coview_url"].endswith(sid)
    chan = fake_transport.channels[-1]
    await settle()
    # PSReadLine is unloaded as the first system input so delta reads stay clean, and the
    # session is handed over at the fresh prompt: the first read sees none of that noise.
    assert any(b"Remove-Module PSReadLine" in w for w in chan.written)
    first = await service.call("term_read", {"session_id": sid})
    assert first["text"] == ""
    await service.call("term_send", {"session_id": sid, "text": "ping -t 127.0.0.1"})
    chan.emit(b"Reply from 127.0.0.1: bytes=32\r\n")
    await settle()
    delta = await service.call("term_read", {"session_id": sid})
    assert "Reply from 127.0.0.1" in delta["text"]
    await service.call("term_send", {"session_id": sid, "keys": ["{ctrl+c}"], "enter": False})
    assert chan.written[-1] == b"\x03"
    screen = await service.call("term_read", {"session_id": sid, "mode": "screen"})
    assert "Reply from 127.0.0.1" in screen["text"]
    listed = await service.call("term_list", {})
    assert listed["sessions"][0]["session_id"] == sid

    def responder(channel: FakeChannel, data: bytes) -> None:
        if b"Write-Output" in data:
            channel.emit(b"hello\r\n" + _marker(data) + b" 0\r\nPS C:\\Users\\dev> ")

    fake_transport.responder = responder
    executed = await service.call("term_exec", {"session_id": sid, "cmd": "echo hello"})
    assert executed["exit_code"] == 0 and executed["state"] == "open"
    assert executed["output"].strip() == "hello" and "note" not in executed

    # A cmdlet-only command: PowerShell has no $LASTEXITCODE yet, so the marker comes back bare.
    def bare(channel: FakeChannel, data: bytes) -> None:
        if b"Write-Output" in data:
            channel.emit(_marker(data) + b" \r\nPS C:\\Users\\dev> ")

    fake_transport.responder = bare
    quiet = await service.call("term_exec", {"session_id": sid, "cmd": "Get-Date"})
    assert quiet["exit_code"] is None and quiet["state"] == "open"
    assert "LASTEXITCODE" in quiet["note"]
    # $LASTEXITCODE keeps the last EXTERNAL program's code, so it must be cleared before the
    # command or a cmdlet-only command reports a stale number (seen live: exit_code 1 on success).
    sent = b"".join(fake_transport.channels[-1].written).decode("utf-8", errors="replace")
    assert "$global:LASTEXITCODE = $null; Get-Date" in sent
    await service.call("term_close", {"session_id": sid})
    assert service.state.term(sid).state == "closed"


async def test_terminal_refused_while_debugger_holds_guest(
    service: NtDriveService, fake_transport: FakeTransport
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    opened = await service.call("term_open", {"vm": "win11-dev"})
    await service.call("kd_attach", {"vm": "win11-dev", "timeout": 5})
    await service.call("kd_break", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("term_read", {"session_id": opened["session_id"]})
    assert exc.value.code == GUEST_FROZEN_BY_DEBUGGER
    assert "kd_go" in exc.value.hint
    await service.call("kd_go", {"vm": "win11-dev"})
    assert (await service.call("term_read", {"session_id": opened["session_id"]}))[
        "state"
    ] == "open"


async def test_term_open_as_the_standard_account(
    service: NtDriveService, fake_transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("term_open", {"vm": "win11-dev", "account": "standard"})
    assert exc.value.code == INVALID_ARGS and "setup-guest.cmd -Standard" in exc.value.hint
    assert fake_transport.opened_as == []  # refused before any SSH login

    cfg = service.config.vms["win11-dev"]
    cfg.guest.standard_user = "ntdrive-user"
    cfg.guest.standard_password = "plain-pw"
    plain = await service.call("term_open", {"vm": "win11-dev", "account": "standard"})
    assert plain["account"] == "standard"
    assert fake_transport.opened_as == [("win11-dev", "standard")]
    listed = await service.call("term_list", {"vm": "win11-dev"})
    assert listed["sessions"][0]["account"] == "standard"
    # The default stays the administrator, on its own SSH connection.
    admin = await service.call("term_open", {"vm": "win11-dev"})
    assert admin["account"] == "admin"
    assert fake_transport.opened_as == [("win11-dev", "standard"), ("win11-dev", "admin")]

    # After a reboot each session is reopened as the account it had.
    import ntdrive.term.manager as manager_mod

    async def fake_wait(host: str, port: int, timeout: float, interval: float = 2.0) -> bool:
        return True

    monkeypatch.setattr(manager_mod, "wait_for_port", fake_wait)
    dropped = service.term.mark_disconnected("win11-dev")
    await service.term.drop_transport("win11-dev")
    successors = await service.term.reopen(cfg, "10.0.0.5", dropped, 1)
    pairs = [
        (service.term.get(old).account, new.account)
        for old, new in zip(dropped, successors, strict=True)
    ]
    assert len(pairs) == 2 and all(old == new for old, new in pairs)


async def test_term_list_names_the_open_sessions_and_prune_drops_the_rest(
    service: NtDriveService,
) -> None:
    await service.call("vm_start", {"vm": "win11-dev"})
    first = (await service.call("term_open", {"vm": "win11-dev"}))["session_id"]
    second = (await service.call("term_open", {"vm": "win11-dev"}))["session_id"]
    await service.call("term_close", {"session_id": first})
    listed = await service.call("term_list", {"vm": "win11-dev"})
    assert {s["session_id"] for s in listed["sessions"]} == {first, second}
    assert listed["open"] == [second]
    pruned = await service.call("term_prune", {"vm": "win11-dev"})
    assert pruned == {"pruned": [first], "remaining": 1}
    # A dropped session (reboot, revert) is stale bookkeeping too once nobody needs its successor.
    service.term.mark_disconnected("win11-dev")
    assert (await service.call("term_list", {}))["open"] == []
    assert (await service.call("term_prune", {}))["pruned"] == [second]
    assert (await service.call("term_list", {}))["sessions"] == []
    with pytest.raises(NtDriveError) as exc:
        await service.call("term_read", {"session_id": second})
    assert exc.value.code == "session_not_found"


async def test_term_exec_ignores_an_echo_chunk_that_ends_at_the_marker(
    service: NtDriveService, fake_transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen live in a PowerShell session right after a reboot: output empty, exit_code null,
    elapsed 16 ms. The PTY echo of the typed line arrived in a chunk that ended right after the
    marker, and the end-of-command regex matched it. The typed line no longer contains the
    marker in one piece, so an echo can never end the command."""
    await service.call("vm_start", {"vm": "win11-dev"})
    sid = (await service.call("term_open", {"vm": "win11-dev"}))["session_id"]
    chan = fake_transport.channels[-1]
    loop = asyncio.get_running_loop()

    def split_write(data: bytes) -> None:
        chan.written.append(data)
        echo = data.replace(b"\r", b"\r\n")
        cut = echo.find(b"__ ") + 3  # the echo pauses right after the marker text and its space
        # A fresh cmd session prints its banner first; nothing before the echo is output either.
        chan.emit(b"Microsoft Windows [Version 10.0]\r\n(c) Microsoft Corporation.\r\n\r\n")
        chan.emit(echo[:cut])
        loop.call_later(0.05, chan.emit, echo[cut:])
        loop.call_later(
            0.1, chan.emit, b"MARKER_4\r\n" + _marker(data) + b" 0\r\nPS C:\\Users\\dev> "
        )

    monkeypatch.setattr(chan, "write", split_write)
    done = await service.call(
        "term_exec", {"session_id": sid, "cmd": '"MARKER_"+(2+2)', "timeout": 5}
    )
    assert done["output"] == "MARKER_4" and done["exit_code"] == 0
    typed = chan.written[-1]
    assert b"__NTDRIVE_" not in typed and b'("__NT" + "DRIVE_' in typed


async def test_wait_until_aborts_when_the_guest_freezes() -> None:
    loop = asyncio.get_running_loop()
    session = TermSession("t-2", "vm", "powershell", "ssh", 40, 5, None, loop)
    chan = FakeChannel(session.on_data_threadsafe, session.on_close_threadsafe, FakeTransport())
    session.attach(chan)
    chan.emit(b"sc start mydrv\r\nstarting")
    await settle()
    # The predicate stands in for kd holding the target at a prompt: the wait must end at once
    # with the output so far, not run to its timeout while the guest cannot answer.
    with pytest.raises(NtDriveError) as exc:
        await session.wait_until("never", timeout=5, abort=lambda: True)
    assert exc.value.code == GUEST_FROZEN_BY_DEBUGGER
    assert "starting" in exc.value.extra["output"]
    chan.close()


async def test_wait_until_honors_max_bytes_and_a_long_backlog() -> None:
    loop = asyncio.get_running_loop()
    session = TermSession("t-3", "vm", "powershell", "ssh", 40, 5, None, loop)
    chan = FakeChannel(session.on_data_threadsafe, session.on_close_threadsafe, FakeTransport())
    session.attach(chan)
    chan.emit(b"x" * 4096 + b"\r\ndone 1\r\n")
    await settle()
    result = await session.wait_until(r"done \d+", timeout=1, max_bytes=512)
    assert result["matched"] == "done 1"
    assert result["truncated"] and len(result["text"]) <= 512
    # More output since the cursor than the scan window: the bounded scan still finds a match
    # that sits at the end, and the window cut keeps the line anchor meaningful.
    start = session.ring.end
    chan.emit(b"y" * (LOOKBACK + 10000) + b"\r\ndone 2\r\n")
    await settle()
    result = await session.wait_until(r"^done \d+", timeout=1, cursor=start)
    assert result["matched"] == "done 2"
    chan.close()

import asyncio

import pytest

from ntdrive.core.service import NtDriveService
from ntdrive.errors import GUEST_FROZEN_BY_DEBUGGER, SESSION_DISCONNECTED, NtDriveError
from ntdrive.term.keys import encode_key_list, encode_keys
from ntdrive.term.session import RingBuffer, TermSession, clean_text

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
    # PSReadLine is unloaded as the first system input so delta reads stay clean.
    assert any(b"Remove-Module PSReadLine" in w for w in chan.written)
    service.term.get(sid).read_delta()  # drain
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
            marker = data.split(b'"')[1].split(b" ")[0]
            channel.emit(b"hello\r\n" + marker + b" 0\r\nPS C:\\Users\\dev> ")

    fake_transport.responder = responder
    executed = await service.call("term_exec", {"session_id": sid, "cmd": "echo hello"})
    assert executed["exit_code"] == 0
    assert executed["output"].strip() == "hello"
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

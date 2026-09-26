"""VNC framebuffer capture: the RFB client, and con_screenshot method=vnc/auto over it."""

from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path

import pytest

from ntdrive.config import Config
from ntdrive.core.service import NtDriveService
from ntdrive.errors import NtDriveError
from ntdrive.screen import vnc

from .conftest import FakeVmrun

# Four known colours for a 2x2 framebuffer: red, green, blue, white (as R,G,B).
PIXELS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]


def _black_png(width: int, height: int) -> bytes:
    """A valid all-black PNG, which is what vmrun captures with no interactive session."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    scanlines = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def test_des_matches_the_classic_vector() -> None:
    # The standard DES test vector proves the block cipher used by VNC auth is correct.
    key = bytes.fromhex("133457799BBCDFF1")
    out = vnc._des(key, bytes.fromhex("0123456789ABCDEF"))
    assert out == bytes.fromhex("85E813540F0AB405")


async def _fake_rfb_server(width: int, height: int) -> tuple[asyncio.AbstractServer, int]:
    """A minimal RFB 3.8 server that serves one raw 2x2 framebuffer with no auth."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            await reader.readexactly(12)  # client version
            writer.write(b"\x01\x01")  # one security type: None
            await writer.drain()
            await reader.readexactly(1)  # chosen type
            writer.write(struct.pack(">I", 0))  # SecurityResult OK
            await reader.readexactly(1)  # ClientInit
            name = b"fake"
            writer.write(
                struct.pack(">HH", width, height)
                + b"\x00" * 16
                + struct.pack(">I", len(name))
                + name
            )
            await writer.drain()
            await reader.readexactly(20)  # SetPixelFormat
            await reader.readexactly(8)  # SetEncodings (one encoding)
            await reader.readexactly(10)  # FramebufferUpdateRequest
            body = bytearray(struct.pack(">BxH", 0, 1))  # FramebufferUpdate, 1 rect
            body += struct.pack(">HHHHi", 0, 0, width, height, 0)  # rect header, Raw
            for r, g, b in PIXELS:
                body += struct.pack("<I", (r << 16) | (g << 8) | b)  # little-endian, R16 G8 B0
            writer.write(bytes(body))
            await writer.drain()
            await asyncio.sleep(0.05)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _decode_png(data: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    idat = b""
    i = 8
    while i < len(data):
        length = struct.unpack(">I", data[i : i + 4])[0]
        tag = data[i + 4 : i + 8]
        chunk = data[i + 8 : i + 8 + length]
        if tag == b"IDAT":
            idat += chunk
        i += 12 + length
    raw = zlib.decompress(idat)
    pixels: list[tuple[int, int, int]] = []
    stride = width * 3
    for y in range(height):
        row = raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]  # skip the filter byte
        pixels.extend((row[x * 3], row[x * 3 + 1], row[x * 3 + 2]) for x in range(width))
    return width, height, pixels


async def test_rfb_capture_writes_a_correct_png(tmp_path: Path) -> None:
    server, port = await _fake_rfb_server(2, 2)
    async with server:
        out = tmp_path / "shot.png"
        await vnc.capture("127.0.0.1", port, "", str(out))
    width, height, pixels = _decode_png(out.read_bytes())
    assert (width, height) == (2, 2) and pixels == PIXELS


async def test_con_enable_vnc_writes_the_vmx_only_while_off(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun
) -> None:
    vm = config.vm("win11-dev")
    Path(vm.vmx).write_text('displayName = "x"\n', encoding="latin-1")
    done = await service.call("con_enable_vnc", {"vm": "win11-dev", "port": 5905})
    assert done["changed"] is True and done["port"] == 5905
    body = Path(vm.vmx).read_text(encoding="latin-1")
    assert (
        'RemoteDisplay.vnc.enabled = "TRUE"' in body and 'RemoteDisplay.vnc.port = "5905"' in body
    )
    assert service.adapter_for(vm).vnc_endpoint(vm) == ("127.0.0.1", 5905)
    again = await service.call("con_enable_vnc", {"vm": "win11-dev", "port": 5905})
    assert again["changed"] is False

    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("con_enable_vnc", {"vm": "win11-dev", "port": 5905})
    assert exc.value.code == "vm_not_running"


async def test_con_screenshot_vnc_reads_the_framebuffer_without_a_guest_login(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, port = await _fake_rfb_server(2, 2)
    async with server:
        vm = config.vm("win11-dev")
        Path(vm.vmx).write_text(
            f'displayName = "x"\nRemoteDisplay.vnc.enabled = "TRUE"\nRemoteDisplay.vnc.port = "{port}"\n',
            encoding="latin-1",
        )
        await service.call("vm_start", {"vm": "win11-dev"})

        shot = await service.call("con_screenshot", {"vm": "win11-dev", "method": "vnc"})
        assert shot["via"] == "vnc" and shot["png_path"].endswith(".png")
        assert _decode_png(Path(shot["png_path"]).read_bytes())[2] == PIXELS

        # auto falls back to VNC when the guest (vmrun) capture fails a guest login.
        async def bad_guest(vm_cfg, out_path):  # type: ignore[no-untyped-def]
            raise NtDriveError("backend_error", "Invalid user name or password for the guest OS")

        monkeypatch.setattr(service.adapter_for(vm), "screenshot", bad_guest)
        auto = await service.call("con_screenshot", {"vm": "win11-dev", "method": "auto"})
        assert auto["via"] == "vnc"

        # auto also falls back when the guest capture SUCCEEDS but returns an all-black frame,
        # which is what vmrun does pre-login and used to be reported as a real screenshot.
        async def blank_guest(vm_cfg, out_path):  # type: ignore[no-untyped-def]
            Path(out_path).write_bytes(_black_png(4, 4))
            return str(out_path)

        monkeypatch.setattr(service.adapter_for(vm), "screenshot", blank_guest)
        blanked = await service.call("con_screenshot", {"vm": "win11-dev", "method": "auto"})
        assert blanked["via"] == "vnc" and "blank" not in blanked
        # method=guest keeps the caller's choice but says the frame is unusable.
        only_guest = await service.call("con_screenshot", {"vm": "win11-dev", "method": "guest"})
        assert only_guest["via"] == "guest" and only_guest["blank"] is True
        assert "method=vnc" in only_guest["note"]


async def _fake_rfb_input_server() -> tuple[
    asyncio.AbstractServer, int, list[tuple[int, bool]], list[tuple[int, int, int]]
]:
    """A minimal RFB 3.8 server that handshakes then records KeyEvent and PointerEvent messages."""
    keys: list[tuple[int, bool]] = []
    pointers: list[tuple[int, int, int]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            await reader.readexactly(12)  # client version
            writer.write(b"\x01\x01")  # one security type: None
            await writer.drain()
            await reader.readexactly(1)  # chosen type
            writer.write(struct.pack(">I", 0))  # SecurityResult OK
            await reader.readexactly(1)  # ClientInit
            name = b"fake"
            writer.write(
                struct.pack(">HH", 4, 4) + b"\x00" * 16 + struct.pack(">I", len(name)) + name
            )
            await writer.drain()
            while True:
                head = await reader.readexactly(1)
                if head[0] == 4:  # KeyEvent: down(1) + padding(2) + keysym(4)
                    body = await reader.readexactly(7)
                    keys.append((struct.unpack(">I", body[3:7])[0], bool(body[0])))
                elif head[0] == 5:  # PointerEvent: mask(1) + x(2) + y(2)
                    body = await reader.readexactly(5)
                    x, y = struct.unpack(">HH", body[1:5])
                    pointers.append((body[0], x, y))
                else:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, keys, pointers


async def test_send_keys_presses_and_releases_over_the_wire() -> None:
    from ntdrive.screen import keymap

    server, port, recorded, _ = await _fake_rfb_input_server()
    async with server:
        events = keymap.flatten(keymap.key_strokes(["A", "{enter}"]))
        sent = await vnc.send_keys("127.0.0.1", port, "", events)
        await asyncio.sleep(0.1)
    assert sent == len(events)
    assert recorded == [
        (0xFFE1, True),  # Shift down
        (0x41, True),  # 'A' down
        (0x41, False),  # 'A' up
        (0xFFE1, False),  # Shift up
        (0xFF0D, True),  # Enter down
        (0xFF0D, False),  # Enter up
    ]


async def test_send_pointer_moves_presses_and_releases() -> None:
    server, port, _, pointers = await _fake_rfb_input_server()
    async with server:
        events = [(0, 10, 20), (1, 10, 20), (0, 10, 20)]
        sent = await vnc.send_pointer("127.0.0.1", port, "", events)
        await asyncio.sleep(0.1)
    assert sent == 3
    assert pointers == [(0, 10, 20), (1, 10, 20), (0, 10, 20)]


async def test_con_click_left_and_double(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun
) -> None:
    server, port, _, pointers = await _fake_rfb_input_server()
    async with server:
        vm = config.vm("win11-dev")
        Path(vm.vmx).write_text(
            f'displayName = "x"\nRemoteDisplay.vnc.enabled = "TRUE"\nRemoteDisplay.vnc.port = "{port}"\n',
            encoding="latin-1",
        )
        await service.call("vm_start", {"vm": "win11-dev"})
        single = await service.call("con_click", {"vm": "win11-dev", "x": 42, "y": 99})
        double = await service.call(
            "con_click", {"vm": "win11-dev", "x": 7, "y": 8, "button": "right", "double": True}
        )
        await asyncio.sleep(0.1)
    assert single == {"vm": "win11-dev", "clicked": [42, 99], "button": "left", "double": False}
    assert double == {"vm": "win11-dev", "clicked": [7, 8], "button": "right", "double": True}
    assert pointers == [
        (0, 42, 99),  # move, left down, left up
        (1, 42, 99),
        (0, 42, 99),
        (0, 7, 8),  # move, right down, right up, right down, right up (double)
        (4, 7, 8),
        (0, 7, 8),
        (4, 7, 8),
        (0, 7, 8),
    ]


async def test_con_click_needs_vnc_enabled(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun
) -> None:
    Path(config.vm("win11-dev").vmx).write_text('displayName = "x"\n', encoding="latin-1")
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("con_click", {"vm": "win11-dev", "x": 1, "y": 1})
    assert exc.value.code == "backend_error" and "con_enable_vnc" in exc.value.hint


async def test_con_send_keys_types_the_password_without_leaking_it(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun
) -> None:
    import json

    server, port, recorded, _ = await _fake_rfb_input_server()
    async with server:
        vm = config.vm("win11-dev")
        Path(vm.vmx).write_text(
            f'displayName = "x"\nRemoteDisplay.vnc.enabled = "TRUE"\nRemoteDisplay.vnc.port = "{port}"\n',
            encoding="latin-1",
        )
        await service.call("vm_start", {"vm": "win11-dev"})
        result = await service.call(
            "con_send_keys", {"vm": "win11-dev", "keys": ["{password}", "{enter}"]}
        )
        await asyncio.sleep(0.1)
    # The result counts items, not characters, so it does not even leak the password length.
    assert result == {"vm": "win11-dev", "sent": 2}
    assert "secret" not in json.dumps(result)
    downs = [ks for ks, down in recorded if down and 0x20 <= ks <= 0x7E]
    assert "".join(chr(k) for k in downs) == "secret"  # guest password from conftest
    assert (0xFF0D, True) in recorded  # then Enter


async def test_con_send_keys_needs_vnc_enabled(
    service: NtDriveService, config: Config, fake_vmrun: FakeVmrun
) -> None:
    Path(config.vm("win11-dev").vmx).write_text('displayName = "x"\n', encoding="latin-1")
    await service.call("vm_start", {"vm": "win11-dev"})
    with pytest.raises(NtDriveError) as exc:
        await service.call("con_send_keys", {"vm": "win11-dev", "keys": ["hi"]})
    assert exc.value.code == "backend_error" and "con_enable_vnc" in exc.value.hint

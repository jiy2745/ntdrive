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


def test_des_matches_the_classic_vector() -> None:
    # The standard DES test vector proves the block cipher used by VNC auth is correct.
    key = bytes.fromhex("133457799BBCDFF1")
    out = vnc._des(key, bytes.fromhex("0123456789ABCDEF"))  # noqa: SLF001
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
        for x in range(width):
            pixels.append((row[x * 3], row[x * 3 + 1], row[x * 3 + 2]))
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

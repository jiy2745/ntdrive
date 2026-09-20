"""A tiny RFB (VNC) client that grabs one framebuffer and writes it as a PNG.

VMware Workstation exposes a VM's console over VNC when `RemoteDisplay.vnc.enabled` is set in the
vmx. That framebuffer is the real screen and reading it needs no guest login, unlike
`vmrun captureScreen`, so it works at a login screen, a boot hang or on an unprovisioned guest.

Only what a one-shot screenshot needs: protocol 3.3/3.7/3.8, the None and VNC-password security
types, a client-chosen 32-bit true-colour pixel format and the Raw encoding. No external
dependencies: the PNG is built with zlib and struct.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import zlib
from collections.abc import Awaitable, Callable

from ntdrive.errors import BACKEND_ERROR, TIMEOUT, NtDriveError


def _vnc_response(challenge: bytes, password: str) -> bytes:
    """The 16-byte DES response to a VNC-auth challenge (the VNC bit-reversed-key quirk)."""
    key = (password.encode("latin-1") + b"\x00" * 8)[:8]
    key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in key)  # VNC reverses each key byte's bits
    return _des(key, challenge[:8]) + _des(key, challenge[8:16])


# -- a minimal DES, only for the 8-byte VNC challenge blocks -------------------------------------
# VNC authentication is the one place the protocol needs DES. Rather than pull in a crypto
# dependency for eight bytes, this is a direct, self-contained ECB DES of one block.
# fmt: off  # DES tables as hex keep the lines short and the appendix compact
_PC1 = bytes.fromhex(
    "39312921191109013a322a221a120a023b332b231b130b033c342c243f372f271f170f073e362e261e160e06"
    "3d352d251d150d051c140c04"
)
_PC2 = bytes.fromhex(
    "0e110b180105031c0f06150a17130c041a0810071b140d0229341f252f371e28332d21302c31273822352e2a"
    "32241d20"
)
_IP = bytes.fromhex(
    "3a322a221a120a023c342c241c140c043e362e261e160e06403830282018100839312921191109013b332b23"
    "1b130b033d352d251d150d053f372f271f170f07"
)
_FP = bytes.fromhex(
    "280830103818402027072f0f37173f1f26062e0e36163e1e25052d0d35153d1d24042c0c34143c1c23032b0b"
    "33133b1b22022a0a32123a1a2101290931113919"
)
_E = bytes.fromhex(
    "20010203040504050607080908090a0b0c0d0c0d0e0f101110111213141514151617181918191a1b1c1d1c1d"
    "1e1f2001"
)
_P = bytes.fromhex("100714151d0c1c11010f171a05121f0a0208180e201b0309130d1e06160b0419")
_SHIFTS = bytes.fromhex("01010202020202020102020202020201")
_SBOX = [
    bytes.fromhex(
        "0e040d01020f0b08030a060c05090007000f07040e020d010a060c0b0905030804010e080d06020b0f0c0907"
        "030a05000f0c080204090107050b030e0a00060d"
    ),
    bytes.fromhex(
        "0f01080e060b03040907020d0c00050a030d04070f02080e0c00010a06090b05000e070b0a040d0105080c06"
        "0903020f0d080a01030f04020b06070c00050e09"
    ),
    bytes.fromhex(
        "0a00090e06030f05010d0c070b0402080d0700090304060a0208050e0c0b0f010d060409080f03000b01020c"
        "050a0e07010a0d0006090807040f0e030b05020c"
    ),
    bytes.fromhex(
        "070d0e030006090a010208050b0c040f0d080b05060f00030407020c010a0e090a0609000c0b070d0f01030e"
        "05020804030f00060a010d080904050b0c07020e"
    ),
    bytes.fromhex(
        "020c0401070a0b060805030f0d000e090e0b020c04070d0105000f0a030908060402010b0a0d07080f090c05"
        "0603000e0b080c07010e020d060f00090a040503"
    ),
    bytes.fromhex(
        "0c010a0f09020608000d03040e07050b0a0f0402070c090506010d0e000b0308090e0f0502080c030700040a"
        "010d0b060403020c09050f0a0b0e01070600080d"
    ),
    bytes.fromhex(
        "040b020e0f00080d030c0907050a06010d000b070409010a0e03050c020f080601040b0d0c03070e0a0f0608"
        "00050902060b0d0801040a070905000f0e02030c"
    ),
    bytes.fromhex(
        "0d020804060f0b010a09030e05000c07010f0d080a0307040c05060b000e0902070b0401090c0e0200060a0d"
        "0f03050802010e07040a080d0f0c09000305060b"
    ),
]
# fmt: on


def _bits(data: bytes) -> list[int]:
    return [(byte >> (7 - i)) & 1 for byte in data for i in range(8)]


def _frombits(bits: list[int]) -> bytes:
    return bytes(sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8))


def _des(key: bytes, block: bytes) -> bytes:
    kb = _bits(key)
    c = [kb[i - 1] for i in _PC1[:28]]
    d = [kb[i - 1] for i in _PC1[28:]]
    subkeys = []
    for shift in _SHIFTS:
        c = c[shift:] + c[:shift]
        d = d[shift:] + d[:shift]
        cd = c + d
        subkeys.append([cd[i - 1] for i in _PC2])
    bb = _bits(block)
    perm = [bb[i - 1] for i in _IP]
    left, right = perm[:32], perm[32:]
    for k in subkeys:
        expanded = [right[i - 1] for i in _E]
        xored = [a ^ b for a, b in zip(expanded, k, strict=True)]
        out: list[int] = []
        for i in range(8):
            chunk = xored[i * 6 : i * 6 + 6]
            row = (chunk[0] << 1) | chunk[5]
            col = (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
            val = _SBOX[i][row * 16 + col]
            out += [(val >> (3 - j)) & 1 for j in range(4)]
        fout = [out[i - 1] for i in _P]
        new_right = [a ^ b for a, b in zip(left, fout, strict=True)]
        left, right = right, new_right
    final = [(right + left)[i - 1] for i in _FP]
    return _frombits(final)


def _png(width: int, height: int, rgb: bytes) -> bytes:
    """A PNG from width*height*3 RGB bytes."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter type 0
        raw += rgb[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


async def _recv(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)


async def capture(host: str, port: int, password: str, out_path: str, timeout: float = 15.0) -> str:
    """Connect to an RFB server, read one full framebuffer and write it as a PNG at out_path."""
    try:
        return await asyncio.wait_for(_capture(host, port, password, out_path), timeout)
    except TimeoutError as exc:
        raise NtDriveError(
            TIMEOUT,
            f"VNC capture from {host}:{port} timed out after {timeout:.0f}s",
            "check that RemoteDisplay.vnc is enabled in the vmx and the VM is running",
        ) from exc
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise NtDriveError(
            BACKEND_ERROR,
            f"VNC capture from {host}:{port} failed: {exc}",
            "enable RemoteDisplay.vnc in the vmx with the VM off, then start it (con_enable_vnc)",
        ) from exc


async def _run_rfb(
    host: str, port: int, verb: str, timeout: float, factory: Callable[[], Awaitable[int]]
) -> int:
    """Run one RFB input session with a timeout, mapping failures the way capture does."""
    try:
        return await asyncio.wait_for(factory(), timeout)
    except TimeoutError as exc:
        raise NtDriveError(
            TIMEOUT,
            f"VNC {verb} to {host}:{port} timed out after {timeout:.0f}s",
            "check that RemoteDisplay.vnc is enabled in the vmx and the VM is running",
        ) from exc
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise NtDriveError(
            BACKEND_ERROR,
            f"VNC {verb} to {host}:{port} failed: {exc}",
            "enable RemoteDisplay.vnc in the vmx with the VM off, then start it (con_enable_vnc)",
        ) from exc


async def send_keys(
    host: str,
    port: int,
    password: str,
    events: list[tuple[int, bool]],
    timeout: float = 15.0,
) -> int:
    """Connect to an RFB server and send KeyEvent messages. Returns the number of events sent."""
    return await _run_rfb(
        host, port, "key input", timeout, lambda: _send_keys(host, port, password, events)
    )


async def send_pointer(
    host: str,
    port: int,
    password: str,
    events: list[tuple[int, int, int]],
    timeout: float = 15.0,
) -> int:
    """Connect to an RFB server and send PointerEvent messages (button mask, x, y).

    Coordinates are framebuffer pixels, the same ones con_screenshot method=vnc captures.
    """
    return await _run_rfb(
        host, port, "pointer input", timeout, lambda: _send_pointer(host, port, password, events)
    )


async def _send_keys(host: str, port: int, password: str, events: list[tuple[int, bool]]) -> int:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        await _handshake(reader, writer, password)
        for keysym, down in events:
            # KeyEvent: message type 4, down-flag, 2 bytes padding, the keysym.
            writer.write(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))
        await writer.drain()
        # Let the server consume the events before the connection drops.
        await asyncio.sleep(0.05)
        return len(events)
    finally:
        writer.close()
        with contextlib.suppress(BaseException):
            await writer.wait_closed()


async def _send_pointer(
    host: str, port: int, password: str, events: list[tuple[int, int, int]]
) -> int:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        await _handshake(reader, writer, password)
        for mask, x, y in events:
            # PointerEvent: message type 5, button mask, x, y.
            writer.write(struct.pack(">BBHH", 5, mask, x, y))
        await writer.drain()
        await asyncio.sleep(0.05)
        return len(events)
    finally:
        writer.close()
        with contextlib.suppress(BaseException):
            await writer.wait_closed()


async def _handshake(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, password: str
) -> tuple[int, int]:
    """RFB version, security, auth and ClientInit. Returns the framebuffer (width, height)."""
    server_version = await _recv(reader, 12)
    major_minor = server_version[4:11].decode("ascii", "replace")
    writer.write(b"RFB 003.008\n" if major_minor >= "003.007" else b"RFB 003.003\n")
    await writer.drain()

    if major_minor >= "003.007":
        count = (await _recv(reader, 1))[0]
        if count == 0:
            raise NtDriveError(BACKEND_ERROR, "VNC server offered no security types")
        types = await _recv(reader, count)
        chosen = 2 if (password and 2 in types) else (1 if 1 in types else types[0])
        writer.write(bytes([chosen]))
        await writer.drain()
    else:
        chosen = struct.unpack(">I", await _recv(reader, 4))[0]

    if chosen == 2:  # VNC authentication
        challenge = await _recv(reader, 16)
        writer.write(_vnc_response(challenge, password))
        await writer.drain()
    if chosen == 2 or major_minor >= "003.008":
        result = struct.unpack(">I", await _recv(reader, 4))[0]
        if result != 0:
            raise NtDriveError(
                BACKEND_ERROR,
                "VNC authentication failed",
                "set the VNC password to match RemoteDisplay.vnc.key, or clear it",
            )

    writer.write(b"\x01")  # ClientInit: shared
    await writer.drain()
    header = await _recv(reader, 24)
    width, height = struct.unpack(">HH", header[:4])
    name_len = struct.unpack(">I", header[20:24])[0]
    await _recv(reader, name_len)
    return width, height


async def _capture(host: str, port: int, password: str, out_path: str) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        width, height = await _handshake(reader, writer, password)
        if not width or not height:
            raise NtDriveError(BACKEND_ERROR, "VNC server reported an empty framebuffer")

        # SetPixelFormat: 32 bpp, depth 24, little-endian (matches the <I decode below),
        # true-colour, R>>16 G>>8 B>>0.
        pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        writer.write(b"\x00\x00\x00\x00" + pixel_format)
        writer.write(struct.pack(">BxH", 2, 1) + struct.pack(">i", 0))  # SetEncodings: Raw only
        writer.write(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))  # full update request
        await writer.drain()

        rgb = await _read_framebuffer(reader, width, height)
        from pathlib import Path

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(_png(width, height, rgb))
        return out_path
    finally:
        writer.close()
        with contextlib.suppress(BaseException):
            await writer.wait_closed()


async def _read_framebuffer(reader: asyncio.StreamReader, width: int, height: int) -> bytes:
    """Read FramebufferUpdate messages until the whole screen has arrived; return RGB bytes."""
    canvas = bytearray(width * height * 3)
    painted = 0
    while painted < width * height:
        msg_type = (await _recv(reader, 1))[0]
        if msg_type != 0:  # only FramebufferUpdate carries pixels; skip the others' fixed bodies
            await _recv(reader, {2: 0, 3: 3}.get(msg_type, 0))
            continue
        num_rects = struct.unpack(">xH", await _recv(reader, 3))[0]
        for _ in range(num_rects):
            x, y, w, h, enc = struct.unpack(">HHHHi", await _recv(reader, 12))
            if enc != 0:
                raise NtDriveError(BACKEND_ERROR, f"VNC sent unsupported encoding {enc}")
            pixels = await _recv(reader, w * h * 4)
            for row in range(h):
                for col in range(w):
                    src = (row * w + col) * 4
                    value = struct.unpack("<I", pixels[src : src + 4])[0]
                    dst = ((y + row) * width + (x + col)) * 3
                    canvas[dst] = (value >> 16) & 0xFF
                    canvas[dst + 1] = (value >> 8) & 0xFF
                    canvas[dst + 2] = value & 0xFF
            painted += w * h
    return bytes(canvas)

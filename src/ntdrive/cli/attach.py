"""`ntdrive term attach`: raw passthrough between the local console and a daemon session."""

from __future__ import annotations

import asyncio
import json
import sys
import threading

import aiohttp

from ntdrive.daemon.client import DaemonClient
from ntdrive.errors import BACKEND_ERROR, NtDriveError

DETACH_KEY = "\x1d"  # Ctrl+]


def _read_keys(queue: asyncio.Queue[bytes | None], loop: asyncio.AbstractEventLoop) -> None:
    """Blocking console reader thread (Windows: msvcrt, elsewhere: raw stdin)."""
    try:
        if sys.platform == "win32":
            import msvcrt

            while True:
                ch = msvcrt.getwch()
                if ch == DETACH_KEY:
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                    return
                if ch in ("\x00", "\xe0"):  # function/arrow prefix: map a few arrows
                    code = msvcrt.getwch()
                    seq = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D"}.get(code, "")
                    data = seq.encode()
                else:
                    data = ch.encode("utf-8")
                loop.call_soon_threadsafe(queue.put_nowait, data)
        else:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            try:
                while True:
                    data = sys.stdin.buffer.read(1)
                    if not data or data == DETACH_KEY.encode():
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                        return
                    loop.call_soon_threadsafe(queue.put_nowait, data)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:  # noqa: BLE001
        loop.call_soon_threadsafe(queue.put_nowait, None)


async def _run(client: DaemonClient, session_id: str) -> None:
    url = client.ws_url(session_id, source="human")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    reader = threading.Thread(target=_read_keys, args=(queue, loop), daemon=True)
    out = sys.stdout.buffer
    async with aiohttp.ClientSession() as http, http.ws_connect(url, heartbeat=20) as ws:
        sys.stderr.write(f"attached to {session_id}; Ctrl+] to detach\n")
        reader.start()

        async def pump_in() -> None:
            while True:
                data = await queue.get()
                if data is None:
                    await ws.close()
                    return
                await ws.send_bytes(data)

        task = asyncio.create_task(pump_in())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    out.write(msg.data)
                    out.flush()
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        info = json.loads(msg.data)
                    except ValueError:
                        continue
                    if info.get("type") in ("state", "error"):
                        sys.stderr.write(f"\n[{info.get('state') or info.get('code')}]\n")
                        if info.get("state") == "disconnected":
                            break
                else:
                    break
        finally:
            task.cancel()
    sys.stderr.write("\ndetached\n")


def attach_session(client: DaemonClient, session_id: str) -> None:
    """Blocking entry point used by the CLI."""
    try:
        asyncio.run(_run(client, session_id))
    except aiohttp.ClientError as exc:
        raise NtDriveError(BACKEND_ERROR, f"websocket failed: {exc}") from exc

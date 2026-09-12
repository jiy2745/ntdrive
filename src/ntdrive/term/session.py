"""A terminal session: ring buffer, virtual screen, read cursors, regex waits and logs.

Every byte the guest emits goes four ways at once: into the ring buffer (for delta reads), into a
pyte screen (for screen reads), to every WebSocket subscriber (CoView, CLI attach) and into the
asciicast log. Input is logged too, tagged with who typed it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

import pyte

from ntdrive.errors import SESSION_DISCONNECTED, TIMEOUT, NtDriveError
from ntdrive.term.transport import TermChannel

ANSI_RE = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI sequences
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    rb"|\x1b[@-Z\\-_]"  # two-byte escapes
    rb"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # other control bytes except \t \n \r
)


class RingBuffer:
    """Bytes with a monotonically increasing absolute offset."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._data = bytearray()
        self.base = 0  # absolute offset of _data[0]

    @property
    def end(self) -> int:
        """Absolute offset one past the last byte."""
        return self.base + len(self._data)

    def append(self, data: bytes) -> None:
        """Append and drop the oldest bytes beyond capacity."""
        self._data += data
        excess = len(self._data) - self.capacity
        if excess > 0:
            del self._data[:excess]
            self.base += excess

    def read(self, cursor: int, max_bytes: int) -> tuple[bytes, int, bool, bool]:
        """Return (bytes, next_cursor, lost_before, truncated_after) from an absolute cursor."""
        lost = cursor < self.base
        start = max(cursor, self.base) - self.base
        chunk = bytes(self._data[start : start + max_bytes])
        next_cursor = self.base + start + len(chunk)
        truncated = next_cursor < self.end
        return chunk, next_cursor, lost, truncated

    def slice_from(self, cursor: int) -> bytes:
        """Everything from the cursor on (used for regex waits)."""
        start = max(cursor, self.base) - self.base
        return bytes(self._data[start:])


def clean_text(data: bytes) -> str:
    """Strip terminal control sequences so an agent sees plain text."""
    stripped = ANSI_RE.sub(b"", data).replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return stripped.decode("utf-8", errors="replace")


class TermSession:
    """One PTY session and everything derived from its byte stream."""

    def __init__(
        self,
        session_id: str,
        vm: str,
        shell: str,
        transport_name: str,
        cols: int,
        rows: int,
        log_path: Path | None,
        loop: asyncio.AbstractEventLoop,
        ring_capacity: int = 1 << 20,
        account: str = "admin",
    ) -> None:
        self.session_id = session_id
        self.vm = vm
        self.shell = shell
        self.transport_name = transport_name
        self.account = account  # which guest account the shell runs as: admin or standard
        self.cols = cols
        self.rows = rows
        self._loop = loop
        self.ring = RingBuffer(ring_capacity)
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.cursor = 0  # default read cursor for callers that do not track their own
        self.opened_at = time.time()
        self.last_activity = self.opened_at
        self.connected = False
        self.successor: str | None = None
        self._channel: TermChannel | None = None
        self._changed = asyncio.Condition()
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._log_path = log_path
        self._events_path = log_path.with_suffix(".events.jsonl") if log_path else None
        self._log_started = time.time()
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            header = {
                "version": 2,
                "width": cols,
                "height": rows,
                "timestamp": int(self._log_started),
                "env": {"TERM": "xterm-256color", "SHELL": shell},
                "title": f"{vm} {session_id}",
            }
            log_path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    # -- wiring -------------------------------------------------------------------------

    def attach(self, channel: TermChannel) -> None:
        """Bind the channel; the transport calls on_data/on_close from its own thread."""
        self._channel = channel
        self.connected = True

    def on_data_threadsafe(self, data: bytes) -> None:
        """Transport thread entry point."""
        self._loop.call_soon_threadsafe(self._feed, data)

    def on_close_threadsafe(self) -> None:
        """Transport thread entry point."""
        self._loop.call_soon_threadsafe(self._mark_disconnected)

    def _feed(self, data: bytes) -> None:
        self.ring.append(data)
        # pyte must never take the session down, so swallow any parser error.
        with contextlib.suppress(Exception):
            self.stream.feed(data)
        self.last_activity = time.time()
        self._log("o", data)
        for queue in list(self._subscribers):
            queue.put_nowait(data)
        self._loop.create_task(self._notify())

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    def _mark_disconnected(self) -> None:
        if not self.connected:
            return
        self.connected = False
        for queue in list(self._subscribers):
            queue.put_nowait(None)
        self._loop.create_task(self._notify())

    def _log(self, kind: str, data: bytes, source: str | None = None) -> None:
        if self._log_path is None:
            return
        t = round(time.time() - self._log_started, 6)
        text = data.decode("utf-8", errors="replace")
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps([t, kind, text]) + "\n")
            if kind == "i" and self._events_path is not None:
                with self._events_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": t, "source": source or "agent", "text": text}) + "\n")
        except OSError:
            pass

    # -- reading ------------------------------------------------------------------------

    @property
    def state(self) -> str:
        """`open` or `disconnected`."""
        return "open" if self.connected else "disconnected"

    def _require_connected(self) -> None:
        if not self.connected:
            hint = (
                f"use session {self.successor} instead"
                if self.successor
                else "open a new session with term_open"
            )
            raise NtDriveError(
                SESSION_DISCONNECTED,
                f"session {self.session_id} is disconnected",
                hint,
                successor=self.successor,
            )

    def read_delta(
        self, cursor: int | None = None, max_bytes: int = 65536, clean: bool = True
    ) -> dict[str, Any]:
        """Output since `cursor` (or since the last default read)."""
        start = self.cursor if cursor is None else cursor
        chunk, next_cursor, lost, truncated = self.ring.read(start, max_bytes)
        if cursor is None:
            self.cursor = next_cursor
        text = clean_text(chunk) if clean else chunk.decode("utf-8", errors="replace")
        return {
            "text": text,
            "cursor": next_cursor,
            "truncated": truncated,
            "lost_before_cursor": lost,
            "state": self.state,
            "successor": self.successor,
        }

    def screen_text(self) -> str:
        """The rendered screen, rows joined by newlines, trailing blanks trimmed."""
        lines = [line.rstrip() for line in self.screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    async def wait_until(
        self, pattern: str, timeout: float, cursor: int | None = None, clean: bool = True
    ) -> dict[str, Any]:
        """Block until `pattern` matches output after `cursor` or the timeout expires."""
        regex = re.compile(pattern, re.MULTILINE)
        start = self.cursor if cursor is None else cursor
        deadline = self._loop.time() + timeout
        while True:
            raw = self.ring.slice_from(start)
            haystack = clean_text(raw) if clean else raw.decode("utf-8", errors="replace")
            match = regex.search(haystack)
            if match:
                result = self.read_delta(cursor, max_bytes=len(raw) + 1, clean=clean)
                result["matched"] = match.group(0)
                return result
            if not self.connected:
                self._require_connected()
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                result = self.read_delta(cursor, clean=clean)
                result["matched"] = None
                result["error"] = {"code": TIMEOUT, "message": "pattern did not appear"}
                return result
            async with self._changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=min(remaining, 1.0))

    # -- writing ------------------------------------------------------------------------

    def send(self, data: bytes, source: str = "agent") -> int:
        """Write input bytes and log who sent them."""
        self._require_connected()
        assert self._channel is not None
        self._channel.write(data)
        self.last_activity = time.time()
        self._log("i", data, source)
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        """Resize PTY and virtual screen."""
        self._require_connected()
        assert self._channel is not None
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)
        self._channel.resize(cols, rows)

    def close(self) -> None:
        """Close the channel; the transport's close callback marks us disconnected."""
        if self._channel is not None:
            self._channel.close()
        self._mark_disconnected()

    # -- streaming ----------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        """Queue that receives every output chunk; None means the session ended."""
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        """Stop streaming to a queue."""
        self._subscribers.discard(queue)

    def snapshot_bytes(self, max_bytes: int = 65536) -> bytes:
        """Recent raw bytes so a new viewer can repaint the screen."""
        chunk, _, _, _ = self.ring.read(max(self.ring.base, self.ring.end - max_bytes), max_bytes)
        return chunk

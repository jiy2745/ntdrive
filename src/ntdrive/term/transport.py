"""Transport interface for terminal sessions.

A transport owns one connection to a guest (for SSH: one TCP connection) and opens PTY channels
on it. Data callbacks may fire from transport-owned threads; TermSession hops them onto the
event loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

DataCallback = Callable[[bytes], None]
CloseCallback = Callable[[], None]


class TermChannel(ABC):
    """One PTY channel."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send input to the guest."""

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Change the PTY size."""

    @abstractmethod
    def close(self) -> None:
        """Close the channel; the close callback fires once."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """False once the channel hit EOF or was closed."""


class TermTransport(ABC):
    """Connection to one guest that can open channels and move files."""

    name: str = "abstract"

    @abstractmethod
    async def open_channel(
        self,
        shell_cmd: str | None,
        cols: int,
        rows: int,
        on_data: DataCallback,
        on_close: CloseCallback,
    ) -> TermChannel:
        """Open a PTY channel running `shell_cmd` (or the default shell when None)."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the connection and every channel."""

    async def put_file(self, local: str, remote: str) -> int:
        """Upload one file; returns bytes copied. Transports without file support raise."""
        raise NotImplementedError

    async def get_file(self, remote: str, local: str) -> int:
        """Download one file; returns bytes copied."""
        raise NotImplementedError

    async def remote_sha256(self, remote: str) -> str | None:
        """Hash of a remote file when the transport can compute it, else None."""
        return None

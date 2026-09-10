"""Python SDK: `NtDrive()` exposes every registry tool as `vt.<group>.<verb>(...)`.

Two modes:
- daemon client (default): talks to ntdrived like the CLI and the MCP server do.
- in-process (`NtDrive(inprocess=True)`): runs a NtDriveService inside this process on a
  background event loop. No daemon is involved, which is what tests and REPL sessions want.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from ntdrive.config import load_config
from ntdrive.core.registry import ToolRegistry, ToolSpec, load_builtin_tools
from ntdrive.core.service import NtDriveService
from ntdrive.daemon.client import DaemonClient, connect
from ntdrive.daemon.lifecycle import read_info
from ntdrive.errors import INVALID_ARGS, TOOL_NOT_FOUND, NtDriveError
from ntdrive.paths import absolutize_local


class _LoopThread:
    """A private event loop running in a daemon thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="ntdrive-sdk", daemon=True
        )
        self._thread.start()

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Run a coroutine on the loop and wait for it."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)


class _Group:
    """`vt.kd`, `vt.term`, ... : attribute access resolves to tool calls."""

    def __init__(self, owner: NtDrive, group: str) -> None:
        self._owner = owner
        self._group = group

    def __getattr__(self, verb: str) -> Any:
        name = f"{self._group}_{verb}"
        spec = self._owner.registry.get(name)
        if spec is None:
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return self._owner.call(name, *args, **kwargs)

        call.__name__ = name
        call.__doc__ = spec.description
        return call

    def __dir__(self) -> list[str]:
        return [spec.verb for spec in self._owner.registry if spec.group == self._group]


class NtDrive:
    """Client object. See the module docstring for the two modes."""

    def __init__(
        self,
        config_path: str | None = None,
        *,
        inprocess: bool = False,
        autostart: bool = True,
        caller: str = "sdk",
        service: NtDriveService | None = None,
    ) -> None:
        self.registry: ToolRegistry = load_builtin_tools()
        self._client: DaemonClient | None = None
        self._service: NtDriveService | None = None
        self._loop: _LoopThread | None = None
        self.caller = caller
        if inprocess or service is not None:
            self._service = service or NtDriveService(load_config(config_path))
            self._loop = _LoopThread()
        else:
            self._client = connect(config_path, autostart=autostart, caller=caller)

    # -- calls --------------------------------------------------------------------------

    def _args(
        self, spec: ToolSpec, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        if len(args) > len(spec.positional):
            raise NtDriveError(
                INVALID_ARGS,
                f"{spec.name} takes at most {len(spec.positional)} positional arguments",
            )
        merged = dict(zip(spec.positional, args, strict=False))
        merged.update(kwargs)
        if isinstance(merged.get("local"), str):
            # The daemon is another process, so host paths must be absolute before they leave.
            merged["local"] = absolutize_local(merged["local"])
        return merged

    def call(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run a tool by name."""
        spec = self.registry.get(name)
        if spec is None:
            raise NtDriveError(TOOL_NOT_FOUND, f"unknown tool {name}")
        payload = self._args(spec, args, kwargs)
        if self._service is not None and self._loop is not None:
            result: dict[str, Any] = self._loop.run(
                self._service.call(name, payload, caller=self.caller)
            )
            return result
        assert self._client is not None
        return self._client.call(name, payload)

    def __getattr__(self, group: str) -> _Group:
        if group.startswith("_") or group not in self.registry.groups():
            raise AttributeError(group)
        return _Group(self, group)

    def tools(self) -> list[dict[str, Any]]:
        """Registry summary."""
        return [spec.summary() for spec in self.registry]

    def daemon_status(self) -> dict[str, Any]:
        """Health of the daemon this client talks to (in-process mode reports itself)."""
        if self._service is not None:
            return {"running": True, "inprocess": True, "version": self._service.version}
        info = read_info()
        if info is None:
            return {"running": False}
        assert self._client is not None
        return {"running": True, "pid": info.pid, "url": info.base_url, **self._client.health()}

    def close(self) -> None:
        """Release in-process resources."""
        if self._service is not None and self._loop is not None:
            self._loop.run(self._service.shutdown())
            self._loop.loop.call_soon_threadsafe(self._loop.loop.stop)


__all__ = ["NtDrive"]

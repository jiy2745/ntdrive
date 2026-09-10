"""DaemonClient: what the MCP server, the CLI and the SDK use to reach ntdrived."""

from __future__ import annotations

from typing import Any

import httpx

from ntdrive.daemon.lifecycle import DaemonInfo, ensure_daemon
from ntdrive.errors import DAEMON_UNAVAILABLE, NtDriveError


class DaemonClient:
    """Thin HTTP client. Tool calls block for as long as the tool does (long polls included)."""

    def __init__(self, info: DaemonInfo, caller: str = "http", timeout: float = 660.0) -> None:
        self.info = info
        self.caller = caller
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        """Auth and caller headers."""
        return {"X-NtDrive-Token": self.info.token, "X-NtDrive-Caller": self.caller}

    def _raise(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            data: Any = resp.json()
        except ValueError:
            data = {"error": {"code": "internal", "message": resp.text[:300]}}
        if resp.status_code >= 400:
            raise NtDriveError.from_dict(data)
        if not isinstance(data, dict):
            return {"result": data}
        return data

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a tool synchronously."""
        try:
            resp = httpx.post(
                f"{self.info.base_url}/api/tools/{name}",
                json=args or {},
                headers=self.headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise NtDriveError(
                DAEMON_UNAVAILABLE, f"cannot reach ntdrived: {exc}", "run `ntdrive daemon status`"
            ) from exc
        return self._raise(resp)

    async def acall(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a tool from an event loop."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.info.base_url}/api/tools/{name}", json=args or {}, headers=self.headers
                )
        except httpx.HTTPError as exc:
            raise NtDriveError(
                DAEMON_UNAVAILABLE, f"cannot reach ntdrived: {exc}", "run `ntdrive daemon status`"
            ) from exc
        return self._raise(resp)

    def tools(self) -> list[dict[str, Any]]:
        """Registry summary from the daemon."""
        resp = httpx.get(f"{self.info.base_url}/api/tools", headers=self.headers, timeout=10)
        data = self._raise(resp)
        tools: list[dict[str, Any]] = data.get("tools", [])
        return tools

    def health(self) -> dict[str, Any]:
        """GET /health."""
        resp = httpx.get(f"{self.info.base_url}/health", timeout=5)
        return self._raise(resp)

    def ws_url(self, session_id: str, source: str = "human") -> str:
        """WebSocket URL for a terminal session."""
        return f"{self.info.ws_base}/ws/term/{session_id}?token={self.info.token}&source={source}"


def connect(
    config_path: str | None = None, autostart: bool = True, caller: str = "http"
) -> DaemonClient:
    """Find or start the daemon and return a client for it."""
    info = ensure_daemon(config_path, autostart=autostart)
    return DaemonClient(info, caller=caller)

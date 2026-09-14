"""DaemonClient: what the MCP server, the CLI and the SDK use to reach ntdrived."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ntdrive.daemon.lifecycle import DaemonInfo, ensure_daemon
from ntdrive.errors import DAEMON_UNAVAILABLE, TIMEOUT, NtDriveError


class DaemonClient:
    """Thin HTTP client. Tool calls block for as long as the tool does (long polls included).

    One httpx client is kept open and reused, which takes a cheap tool from about 20 ms to
    about 5 ms. The object holds no daemon state: daemon.json is the state. After `ntdrive
    daemon restart` a call that comes back 401 (new token) or cannot connect (new port)
    re-reads daemon.json once and retries, so a restart is transparent to the CLI, the SDK and
    a running MCP server (live kd and terminal sessions are gone all the same).
    """

    def __init__(
        self,
        info: DaemonInfo,
        caller: str = "http",
        timeout: float = 660.0,
        config_path: str | None = None,
    ) -> None:
        self.info = info
        self.caller = caller
        self.timeout = timeout
        self.config_path = config_path
        self._sync: httpx.Client | None = None
        self._async: httpx.AsyncClient | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None

    @property
    def headers(self) -> dict[str, str]:
        """Auth and caller headers."""
        return {"X-NtDrive-Token": self.info.token, "X-NtDrive-Caller": self.caller}

    def _sync_client(self) -> httpx.Client:
        if self._sync is None:
            self._sync = httpx.Client(base_url=self.info.base_url, timeout=self.timeout)
        return self._sync

    def _async_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._async is None or self._async_loop is not loop:
            self._async = httpx.AsyncClient(base_url=self.info.base_url, timeout=self.timeout)
            self._async_loop = loop
        return self._async

    def _refresh(self) -> bool:
        """Re-read daemon.json after a 401 or a refused connection. True when it changed."""
        try:
            info = ensure_daemon(self.config_path, autostart=False)
        except NtDriveError:
            return False
        changed = info.token != self.info.token or info.base_url != self.info.base_url
        self.info = info
        if changed:
            self.close()
        return changed

    def close(self) -> None:
        """Drop the kept connections. The async one is closed by aclose() on its own loop."""
        if self._sync is not None:
            self._sync.close()
            self._sync = None
        self._async = None
        self._async_loop = None

    async def aclose(self) -> None:
        """Close the async connection, from the loop that opened it."""
        client, self._async, self._async_loop = self._async, None, None
        if client is not None:
            await client.aclose()

    @staticmethod
    def _unavailable(exc: Exception) -> NtDriveError:
        return NtDriveError(
            DAEMON_UNAVAILABLE, f"cannot reach ntdrived: {exc}", "run `ntdrive daemon status`"
        )

    def _timed_out(self, name: str) -> NtDriveError:
        return NtDriveError(
            TIMEOUT,
            f"{name} gave no answer within {self.timeout:.0f}s and may still be running in "
            "the daemon",
            "call sys_state (or kd_state, term_list) to see where it got, then retry with a "
            "smaller timeout",
        )

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
        retried = False
        while True:
            try:
                resp = self._sync_client().post(
                    f"/api/tools/{name}", json=args or {}, headers=self.headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if retried or not self._refresh():
                    raise self._unavailable(exc) from exc
                retried = True
                continue
            except httpx.TimeoutException:
                raise self._timed_out(name) from None
            except httpx.HTTPError as exc:
                raise self._unavailable(exc) from exc
            if resp.status_code == 401 and not retried and self._refresh():
                retried = True
                continue
            return self._raise(resp)

    async def acall(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a tool from an event loop."""
        retried = False
        while True:
            client = self._async_client()
            try:
                resp = await client.post(
                    f"/api/tools/{name}", json=args or {}, headers=self.headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if retried or not await asyncio.to_thread(self._refresh):
                    raise self._unavailable(exc) from exc
                await client.aclose()
                retried = True
                continue
            except httpx.TimeoutException:
                raise self._timed_out(name) from None
            except httpx.HTTPError as exc:
                raise self._unavailable(exc) from exc
            if resp.status_code == 401 and not retried and await asyncio.to_thread(self._refresh):
                await client.aclose()
                retried = True
                continue
            return self._raise(resp)

    def tools(self) -> list[dict[str, Any]]:
        """Registry summary from the daemon."""
        resp = self._sync_client().get("/api/tools", headers=self.headers, timeout=10)
        data = self._raise(resp)
        tools: list[dict[str, Any]] = data.get("tools", [])
        return tools

    def health(self) -> dict[str, Any]:
        """GET /health."""
        resp = self._sync_client().get("/health", timeout=5)
        return self._raise(resp)

    def ws_url(self, session_id: str, source: str = "human") -> str:
        """WebSocket URL for a terminal session."""
        return f"{self.info.ws_base}/ws/term/{session_id}?token={self.info.token}&source={source}"


def connect(
    config_path: str | None = None, autostart: bool = True, caller: str = "http"
) -> DaemonClient:
    """Find or start the daemon and return a client for it."""
    info = ensure_daemon(config_path, autostart=autostart)
    return DaemonClient(info, caller=caller, config_path=config_path)

"""TermManager: opens sessions over a transport, tracks them, reconnects after reboots."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ntdrive.config import HostConfig, VmConfig, state_dir
from ntdrive.core.state import StateStore, TermInfo, TermState
from ntdrive.errors import BACKEND_UNSUPPORTED, SESSION_NOT_FOUND, TIMEOUT, NtDriveError
from ntdrive.term.session import TermSession
from ntdrive.term.ssh import SHELL_COMMANDS, SshPtyTransport, wait_for_port
from ntdrive.term.transport import TermTransport

TransportFactory = Callable[[VmConfig, str], TermTransport]

# PSReadLine redraws the input line on every keystroke, which floods delta reads with echo
# fragments ("ping -t ping -t 1ping -t 12..."). Unloading it gives a plain line editor.
PSREADLINE_OFF = "Remove-Module PSReadLine -ErrorAction SilentlyContinue; Clear-Host\r"
# A PowerShell prompt at the end of the output means the shell is ready for the next line.
PROMPT_READY = r"^PS [^\r\n]*> ?$"
SHELL_READY_TIMEOUT = 5.0


def default_transport_factory(host: HostConfig) -> TransportFactory:
    """Build SSH transports from the VM config."""

    def factory(vm: VmConfig, ip: str) -> TermTransport:
        return SshPtyTransport(
            ip,
            vm.guest.ssh_port,
            vm.guest.user,
            vm.guest.resolve_password(),
            connect_timeout=host.ssh_connect_timeout,
            # Pinned per VM so a reverted or rebooted guest keeps working and a stranger on the
            # same DHCP address is refused before the password is sent.
            host_key_file=state_dir() / "hostkeys" / f"{vm.name}.json",
        )

    return factory


class TermManager:
    """Owns every TermSession and the per-VM transports."""

    def __init__(
        self,
        host: HostConfig,
        state: StateStore,
        log_dir: Path,
        transport_factory: TransportFactory | None = None,
        coview_base: str = "",
    ) -> None:
        self.host = host
        self.state = state
        self.log_dir = log_dir
        self._factory = transport_factory or default_transport_factory(host)
        self.coview_base = coview_base
        self._sessions: dict[str, TermSession] = {}
        self._transports: dict[str, TermTransport] = {}
        self._ips: dict[str, str] = {}

    # -- lookup -------------------------------------------------------------------------

    def get(self, session_id: str) -> TermSession:
        """Session by id, or session_not_found."""
        try:
            return self._sessions[session_id]
        except KeyError:
            raise NtDriveError(
                SESSION_NOT_FOUND,
                f"no terminal session {session_id}",
                "call term_list to see live sessions or term_open to create one",
            ) from None

    def sessions(self, vm: str | None = None) -> list[dict[str, Any]]:
        """Session summaries, optionally for one VM."""
        out: list[dict[str, Any]] = []
        for runtime in self.state.all():
            if vm and runtime.name != vm:
                continue
            for info in runtime.terms.values():
                out.append(info.to_dict())
        return out

    def transport_for(self, vm: VmConfig) -> TermTransport | None:
        """The live transport for a VM, if any (used by file tools)."""
        return self._transports.get(vm.name)

    async def transport(self, vm: VmConfig, ip: str) -> TermTransport:
        """Transport for a VM, creating it on first use or when the IP changed."""
        existing = self._transports.get(vm.name)
        if existing is not None and self._ips.get(vm.name) == ip:
            return existing
        if existing is not None:
            await existing.close()
        transport = self._factory(vm, ip)
        self._transports[vm.name] = transport
        self._ips[vm.name] = ip
        return transport

    # -- lifecycle ----------------------------------------------------------------------

    async def open(
        self,
        vm: VmConfig,
        ip: str,
        shell: str,
        cols: int,
        rows: int,
        transport_kind: str = "auto",
    ) -> TermSession:
        """Open a new PTY session on the VM."""
        if transport_kind not in ("auto", "ssh"):
            raise NtDriveError(
                BACKEND_UNSUPPORTED,
                f"transport {transport_kind} is not available in this version",
                "use transport=ssh",
            )
        transport = await self.transport(vm, ip)
        session_id = f"t-{secrets.token_hex(4)}"
        loop = asyncio.get_running_loop()
        log_path = self.log_dir / "term" / f"{vm.name}-{session_id}.cast"
        session = TermSession(
            session_id, vm.name, shell, transport.name, cols, rows, log_path, loop
        )
        channel = await transport.open_channel(
            SHELL_COMMANDS.get(shell),
            cols,
            rows,
            session.on_data_threadsafe,
            lambda: self._on_channel_closed(session),
        )
        session.attach(channel)
        self._sessions[session_id] = session
        info = TermInfo(
            session_id=session_id,
            vm=vm.name,
            shell=shell,
            transport=transport.name,
            coview_url=f"{self.coview_base}#{session_id}" if self.coview_base else "",
        )
        self.state.vm(vm.name).terms[session_id] = info
        if shell in ("powershell", "pwsh"):
            with contextlib.suppress(NtDriveError):
                start = session.ring.end
                session.send(PSREADLINE_OFF.encode(), source="system")
                # The shell needs a moment to unload PSReadLine and clear the screen, and a
                # command typed meanwhile can be swallowed (seen live: the first term_exec after
                # term_open timed out). Wait for the fresh prompt and hand the session over
                # there, so the agent's first read does not see the setup noise either.
                ready = await session.wait_until(PROMPT_READY, SHELL_READY_TIMEOUT, cursor=start)
                if ready.get("matched"):
                    session.cursor = ready["cursor"]
        return session

    def _on_channel_closed(self, session: TermSession) -> None:
        session.on_close_threadsafe()
        loop = session._loop  # noqa: SLF001 - manager and session are one unit
        loop.call_soon_threadsafe(self._set_state, session.session_id, TermState.DISCONNECTED)

    def _set_state(self, session_id: str, state: TermState) -> None:
        info = self.state.term(session_id)
        if info is not None and info.state != TermState.CLOSED:
            info.state = state

    async def close(self, session_id: str) -> None:
        """Close one session."""
        session = self.get(session_id)
        session.close()
        self._set_state(session_id, TermState.CLOSED)

    def mark_disconnected(self, vm: str) -> list[str]:
        """Flag every open session of a VM as disconnected (reboot, revert)."""
        dropped: list[str] = []
        for sid, session in self._sessions.items():
            if session.vm == vm and session.connected:
                session.close()
                self._set_state(sid, TermState.DISCONNECTED)
                dropped.append(sid)
        return dropped

    async def drop_transport(self, vm: str) -> None:
        """Forget the SSH connection for a VM (its TCP state is stale after a revert)."""
        transport = self._transports.pop(vm, None)
        self._ips.pop(vm, None)
        if transport is not None:
            await transport.close()

    async def reopen(
        self, vm: VmConfig, ip: str, old_session_ids: list[str], timeout: float
    ) -> list[TermSession]:
        """Wait for SSH and open a successor for each dropped session."""
        if not old_session_ids:
            return []
        ok = await wait_for_port(ip, vm.guest.ssh_port, timeout)
        if not ok:
            raise NtDriveError(
                TIMEOUT,
                f"ssh on {ip}:{vm.guest.ssh_port} did not come back within {timeout:.0f}s",
                "call term_open once the guest is up",
            )
        await self.drop_transport(vm.name)
        successors: list[TermSession] = []
        for old_id in old_session_ids:
            old = self._sessions.get(old_id)
            shell = old.shell if old else vm.guest.shell
            cols = old.cols if old else 120
            rows = old.rows if old else 40
            new = await self.open(vm, ip, shell, cols, rows)
            if old is not None:
                old.successor = new.session_id
            info = self.state.term(old_id)
            if info is not None:
                info.successor = new.session_id
            successors.append(new)
        return successors

    async def close_all(self) -> None:
        """Daemon shutdown: close channels and connections."""
        for session in list(self._sessions.values()):
            session.close()
        for transport in list(self._transports.values()):
            await transport.close()
        self._transports.clear()

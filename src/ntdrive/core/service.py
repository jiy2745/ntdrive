"""NtDriveService: owns adapters, sessions and state, and dispatches tool calls.

`call(name, args)` is the one entry point every front door uses: it validates the arguments
against the tool's parameter model, runs the policy gate, caps long-poll timeouts, invokes the
handler and writes an audit record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ntdrive import __version__
from ntdrive.config import Config, PolicyConfig, VmConfig, load_policy
from ntdrive.core.audit import AuditLog
from ntdrive.core.policy import Policy
from ntdrive.core.registry import ToolRegistry, load_builtin_tools
from ntdrive.core.state import KdState, PowerState, StateStore, VmRuntime
from ntdrive.errors import (
    BACKEND_UNSUPPORTED,
    GUEST_FROZEN_BY_DEBUGGER,
    INTERNAL,
    INVALID_ARGS,
    TIMEOUT,
    TOOL_NOT_FOUND,
    VM_NOT_RUNNING,
    NtDriveError,
)
from ntdrive.hypervisor.base import HypervisorAdapter
from ntdrive.hypervisor.vmware import VmwareAdapter
from ntdrive.kd import firewall as host_firewall
from ntdrive.kd.firewall import FirewallCheck, FirewallFix, FirewallStatus
from ntdrive.kd.session import Breaker, KdSession, PipeCheck, Spawner, named_pipe_exists
from ntdrive.term.manager import TermManager, TransportFactory
from ntdrive.term.ssh import probe_tcp_port
from ntdrive.term.transport import TermTransport

# A readable firewall answer is kept this long: the read enumerates every rule on the host.
FIREWALL_CACHE_TTL = 30.0


class NtDriveService:
    """Everything the daemon holds in memory, plus the dispatcher."""

    def __init__(
        self,
        config: Config,
        *,
        adapters: dict[str, HypervisorAdapter] | None = None,
        policy: PolicyConfig | None = None,
        transport_factory: TransportFactory | None = None,
        kd_spawner: Spawner | None = None,
        kd_breaker: Breaker | None = None,
        kd_pipe_check: PipeCheck | None = None,
        firewall_check: FirewallCheck | None = None,
        firewall_fix: FirewallFix | None = None,
        log_dir: Path | None = None,
        coview_base: str = "",
    ) -> None:
        self.config = config
        self.version = __version__
        self.log_dir = log_dir or config.host.resolved_log_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state = StateStore()
        self.audit = AuditLog(self.log_dir / "audit.jsonl", self.state.t_plus)
        self.policy = Policy(policy if policy is not None else load_policy(config))
        self.registry: ToolRegistry = load_builtin_tools()
        self.adapters: dict[str, HypervisorAdapter] = adapters or {
            "vmware": VmwareAdapter(config.host.vmrun)
        }
        self.term = TermManager(
            config.host,
            self.state,
            self.log_dir,
            transport_factory=transport_factory,
            coview_base=coview_base,
        )
        self.kd_sessions: dict[str, KdSession] = {}
        self._kd_spawner = kd_spawner
        self._kd_breaker = kd_breaker
        self._kd_pipe_check = kd_pipe_check
        self._firewall_check = firewall_check
        self._firewall_fix = firewall_fix
        self._firewall_cache: tuple[float, FirewallStatus] | None = None
        self._snapshot_meta_dir = self.log_dir / "snapshots"
        # Fast TCP probe used before trying SFTP. Tests replace it to avoid real sockets.
        self.ssh_probe: Callable[[str, int], Awaitable[bool]] = probe_tcp_port

    # -- dispatch -----------------------------------------------------------------------

    async def call(self, name: str, args: dict[str, Any], caller: str = "local") -> dict[str, Any]:
        """Validate, gate, run and audit one tool call."""
        spec = self.registry.get(name)
        if spec is None:
            raise NtDriveError(
                TOOL_NOT_FOUND,
                f"unknown tool {name}",
                f"known tools: {', '.join(self.registry.names())}",
            )
        started = time.monotonic()
        try:
            try:
                params = spec.params.model_validate(args or {})
            except ValidationError as exc:
                issues = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
                raise NtDriveError(
                    INVALID_ARGS, f"invalid arguments for {name}: {issues}"
                ) from None
            self.policy.check(spec, params)
            if spec.long_poll and hasattr(params, "timeout"):
                cap = float(self.config.host.tool_timeout_max)
                if float(params.timeout) > cap:
                    params.timeout = cap
            result = await spec.handler(self, params)
            self.audit.record(
                name,
                args,
                caller=caller,
                ok=True,
                elapsed_ms=(time.monotonic() - started) * 1000,
                result=result,
            )
            return result
        except NtDriveError as exc:
            self.audit.record(
                name,
                args,
                caller=caller,
                ok=False,
                elapsed_ms=(time.monotonic() - started) * 1000,
                error=exc.to_dict()["error"],
            )
            raise
        except TimeoutError as exc:
            err = NtDriveError(TIMEOUT, f"{name} timed out")
            self.audit.record(
                name, args, caller=caller, ok=False, elapsed_ms=0, error=err.to_dict()["error"]
            )
            raise err from exc
        except Exception as exc:  # noqa: BLE001 - convert anything else into a wire error
            err = NtDriveError(INTERNAL, f"{name} failed: {type(exc).__name__}: {exc}")
            self.audit.record(
                name, args, caller=caller, ok=False, elapsed_ms=0, error=err.to_dict()["error"]
            )
            raise err from exc

    # -- lookups ------------------------------------------------------------------------

    def vm_cfg(self, name: str) -> VmConfig:
        """VM config, checking that the backend is supported."""
        cfg = self.config.vm(name)
        if cfg.backend not in self.adapters:
            raise NtDriveError(
                BACKEND_UNSUPPORTED,
                f"backend '{cfg.backend}' for VM {name} is not available in this version",
                "only backend: vmware is supported",
            )
        return cfg

    def adapter_for(self, vm: VmConfig) -> HypervisorAdapter:
        """Adapter for a VM's backend."""
        return self.adapters[vm.backend]

    def runtime(self, name: str) -> VmRuntime:
        """Runtime state, synchronized with the live kd session."""
        runtime = self.state.vm(name)
        cfg = self.config.vms.get(name)
        if cfg is not None:
            runtime.kd_transport = cfg.kd_transport
            runtime.kd_serial_pipe = (
                cfg.resolved_serial_pipe() if cfg.kd_transport == "serial" else None
            )
            runtime.kd_port = cfg.kdnet.port if cfg.kd_transport == "net" else None
        session = self.kd_sessions.get(name)
        if session is not None:
            runtime.kd_state = session.state
            runtime.kd_transport = session.transport
            runtime.kd_port = session.port if session.transport == "net" else None
            runtime.kd_serial_pipe = session.serial_pipe if session.transport == "serial" else None
            runtime.kd_target_info = session.target_info
            runtime.kd_last_event = session.last_event
            runtime.kd_log_path = str(session.log_path)
        return runtime

    async def refresh_power(self, vm: VmConfig) -> PowerState:
        """Ask the hypervisor and cache the answer."""
        power = await self.adapter_for(vm).power_state(vm)
        self.state.vm(vm.name).power = power
        return power

    async def ensure_running(self, vm: VmConfig) -> None:
        """Raise vm_not_running unless the VM is powered on."""
        if await self.refresh_power(vm) != PowerState.RUNNING:
            raise NtDriveError(
                VM_NOT_RUNNING, f"VM {vm.name} is not running", "call vm_start first"
            )

    def ensure_not_frozen(self, vm_name: str) -> None:
        """Raise guest_frozen_by_debugger while kd holds the target at a prompt."""
        if self.runtime(vm_name).kd_state == KdState.BROKEN:
            raise NtDriveError(
                GUEST_FROZEN_BY_DEBUGGER,
                f"the debugger holds {vm_name} at a kd> prompt; the guest cannot respond",
                "call kd_go (or finish debugging) and retry",
            )

    def kd_session(
        self, vm: VmConfig, port: int | None = None, key: str | None = None
    ) -> KdSession:
        """The KdSession for a VM, created on first use."""
        session = self.kd_sessions.get(vm.name)
        if session is None:
            session = KdSession(
                vm.name,
                self.config.host.kd,
                port or vm.kdnet.port,
                key or vm.kdnet.key,
                self.config.host.symbol_path,
                self.log_dir / "kd" / f"{vm.name}.log",
                asyncio.get_running_loop(),
                spawner=self._kd_spawner,
                breaker=self._kd_breaker,
                transport=vm.kd_transport,
                serial_pipe=vm.resolved_serial_pipe(),
                pipe_check=self._kd_pipe_check,
            )
            self.kd_sessions[vm.name] = session
        if port is not None:
            session.port = port
        if key is not None:
            session.key = key
        return session

    async def guest_ip(self, vm: VmConfig, timeout: float = 60.0) -> str:
        """Guest IPv4 through the hypervisor tools."""
        return await self.adapter_for(vm).guest_ip(vm, timeout=timeout)

    async def ssh_reachable(self, vm: VmConfig, timeout: float = 60.0) -> tuple[bool, str]:
        """(True, ip) when the guest SSH port accepts connections within about two seconds.

        `timeout` bounds the IP lookup, which blocks while VMware Tools report no address.
        """
        ip = await self.guest_ip(vm, timeout=timeout)
        return await self.ssh_probe(ip, vm.guest.ssh_port), ip

    def serial_pipe_open(self, pipe: str) -> bool:
        """True when the host side of a serial named pipe has a server (the VM exposes COM1)."""
        return (self._kd_pipe_check or named_pipe_exists)(pipe)

    async def kdnet_firewall(self) -> FirewallStatus:
        """Whether the host firewall lets kd.exe receive KDNET packets. Needs no privilege.

        A readable answer is cached for FIREWALL_CACHE_TTL seconds, a repair replaces it, and
        an unreadable answer is not kept so the next call tries again.
        """
        now = time.monotonic()
        if self._firewall_cache is not None:
            at, cached = self._firewall_cache
            if now - at < FIREWALL_CACHE_TTL:
                return cached
        check = self._firewall_check or host_firewall.firewall_status
        status = await check(self.config.host.kd)
        if status.checked:
            self._firewall_cache = (now, status)
        return status

    async def fix_kdnet_firewall(self, timeout: float) -> FirewallStatus:
        """Drop the Block rules for kd.exe and add the Allow rule through one UAC prompt."""
        fix = self._firewall_fix or host_firewall.firewall_fix
        self._firewall_cache = None
        status = await fix(self.config.host.kd, timeout)
        if status.checked:
            self._firewall_cache = (time.monotonic(), status)
        return status

    async def transport(self, vm: VmConfig) -> TermTransport:
        """Transport to the guest, resolving the IP when needed."""
        existing = self.term.transport_for(vm)
        if existing is not None:
            return existing
        ip = await self.guest_ip(vm)
        return await self.term.transport(vm, ip)

    async def file_transport(self, vm: VmConfig) -> TermTransport | None:
        """Transport for SFTP, or None when the guest has no reachable SSH.

        A cached transport is reused as is (it may carry live terminal channels, so it is never
        closed or replaced here). Without one, the SSH port is probed once (about two seconds)
        instead of paying a full SSH timeout per file when the guest has no OpenSSH yet.
        """
        existing = self.term.transport_for(vm)
        if existing is not None:
            return existing
        ssh_ok, ip = await self.ssh_reachable(vm)
        if not ssh_ok:
            return None
        return await self.term.transport(vm, ip)

    async def release_guest(self, vm_name: str) -> dict[str, Any]:
        """Before a suspend or stop: detach kd (resuming a broken target) and drop terminals.

        Returns what was torn down so the caller can report it or restore it afterwards.
        """
        kd = self.kd_sessions.get(vm_name)
        was_attached = kd is not None and kd.attached
        if was_attached and kd is not None:
            await kd.detach()
        dropped = self.term.mark_disconnected(vm_name)
        await self.term.drop_transport(vm_name)
        return {"kd_was_attached": was_attached, "terms_dropped": dropped}

    # -- snapshot metadata --------------------------------------------------------------

    def _snapshot_meta_path(self, vm: str) -> Path:
        self._snapshot_meta_dir.mkdir(parents=True, exist_ok=True)
        return self._snapshot_meta_dir / f"{vm}.json"

    def load_snapshot_meta(self, vm: str) -> dict[str, dict[str, Any]]:
        """Descriptions and tags recorded by snap_take (vmrun has no description field)."""
        path = self._snapshot_meta_path(vm)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_snapshot_meta(self, vm: str, meta: dict[str, dict[str, Any]]) -> None:
        """Persist snapshot metadata."""
        self._snapshot_meta_path(vm).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # -- shutdown -----------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Release the guest (kd go), stop kd.exe and close SSH connections."""
        for session in list(self.kd_sessions.values()):
            with contextlib.suppress(NtDriveError):
                await session.detach()
        await self.term.close_all()

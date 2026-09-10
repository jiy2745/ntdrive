"""VMware Workstation adapter built on vmrun.exe.

vmrun is the only backend path: the Workstation REST API has no snapshot support. Every call is
`vmrun -T ws [-gu user -gp pass] <command> <vmx> [args]`. Read-only commands are retried because
vmrun fails intermittently when the Workstation UI process is busy.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ntdrive.config import VmConfig
from ntdrive.core.state import PowerState
from ntdrive.errors import (
    BACKEND_ERROR,
    INVALID_ARGS,
    REASON_CONFIG_UNREADABLE,
    REASON_ENCRYPTED_LIVE,
    REASON_PASSWORD_REQUIRED,
    REASON_SNAPSHOT_MISSING,
    TIMEOUT,
    VM_NOT_RUNNING,
    NtDriveError,
)
from ntdrive.hypervisor.base import HypervisorAdapter, SnapshotNode, SnapshotTree

Runner = Callable[[list[str], float], Awaitable[tuple[int, str]]]

VERSION_RE = re.compile(r"vmrun version ([\w.\-]+)", re.IGNORECASE)
log = logging.getLogger("ntdrive.vmware")
SECRET_FLAGS = {"-vp", "-gp"}


def _mask_argv(argv: list[str]) -> str:
    """Command line for logs with the passwords that follow -vp and -gp replaced."""
    out: list[str] = []
    hide = False
    for item in argv:
        out.append("***" if hide else item)
        hide = item in SECRET_FLAGS
    return " ".join(out)


async def subprocess_runner(args: list[str], timeout: float) -> tuple[int, str]:
    """Run a command and return (exit code, combined stdout+stderr).

    The daemon runs without a console, so on Windows every child would otherwise get a console
    window of its own and flash it on the desktop. CREATE_NO_WINDOW keeps vmrun invisible.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise NtDriveError(
            TIMEOUT, f"{Path(args[0]).name} timed out after {timeout:.0f}s"
        ) from None
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _norm(path: str) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return path.lower()


def classify_vmrun_error(output: str) -> tuple[str, str]:
    """(reason tag, hint) for a failed vmrun call. This is the one place VIX text is matched."""
    low = output.lower()
    if "encrypted virtual machine failed" in low:
        # Researched on Workstation 17.6: vmrun cannot take a LIVE (memory) snapshot of a RUNNING
        # encrypted VM (even partial encryption for a vTPM). The same -vp password works for
        # start, list, delete, revert, screenshot and file copy, and a snapshot of the VM while
        # POWERED OFF works too. Only the running-plus-memory case is refused.
        return REASON_ENCRYPTED_LIVE, (
            "vmrun cannot create or delete a memory snapshot of a running encrypted VM. "
            "Pass allow_suspend=true to suspend, run the operation and resume, or do it "
            "while the VM is powered off."
        )
    if "a password is required" in low:
        return REASON_PASSWORD_REQUIRED, (
            "the VM is encrypted. Set encryption_password_env (preferred) or "
            "encryption_password in vms.yaml."
        )
    if "cannot read the virtual machine configuration file" in low:
        # Seen for a second or two right after a suspend while Workstation rewrites the files.
        return REASON_CONFIG_UNREADABLE, "retry in a moment; the vmx is being rewritten"
    if "the snapshot does not exist" in low:
        return REASON_SNAPSHOT_MISSING, "call snap_list for the names that exist"
    return "", "check that VMware Workstation is installed and the vmx path is right"


def parse_snapshot_tree(output: str) -> list[SnapshotNode]:
    """Parse `vmrun listSnapshots <vmx> showTree` output into a forest.

    The first line is `Total snapshots: N`. Children are indented (vmrun uses tabs, but any
    consistent indentation works because nesting is derived from indent width).
    """
    roots: list[SnapshotNode] = []
    stack: list[tuple[int, SnapshotNode]] = []
    for raw in output.splitlines():
        if not raw.strip() or raw.lower().startswith("total snapshots"):
            continue
        stripped = raw.lstrip("\t ")
        indent = len(raw) - len(stripped)
        node = SnapshotNode(name=stripped.rstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((indent, node))
    return roots


def parse_current_snapshot(vmsd_text: str) -> str | None:
    """Find the display name of the snapshot a VM currently descends from (from the .vmsd)."""
    current: str | None = None
    uids: dict[str, str] = {}
    names: dict[str, str] = {}
    for line in vmsd_text.splitlines():
        m = re.match(r'\s*([\w.]+)\s*=\s*"(.*)"\s*$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key == "snapshot.current":
            current = value
        elif key.endswith(".uid") and key.startswith("snapshot"):
            uids[key[: -len(".uid")]] = value
        elif key.endswith(".displayName") and key.startswith("snapshot"):
            names[key[: -len(".displayName")]] = value
    if current is None:
        return None
    for prefix, uid in uids.items():
        if uid == current:
            return names.get(prefix)
    return None


class VmwareAdapter(HypervisorAdapter):
    """HypervisorAdapter implementation for VMware Workstation."""

    backend = "vmware"

    def __init__(
        self,
        vmrun_path: str,
        runner: Runner | None = None,
        retries: int = 3,
        default_timeout: float = 60.0,
    ) -> None:
        self.vmrun_path = vmrun_path
        self._runner = runner or subprocess_runner
        self.retries = retries
        self.default_timeout = default_timeout

    def capabilities(self) -> list[str]:
        """Feature flags for sys_health."""
        return ["power", "snapshot_tree", "guest_ip", "screenshot", "copy_file", "run_in_guest"]

    async def _exec(
        self,
        command: str,
        vm: VmConfig | None = None,
        *args: str,
        guest_auth: bool = False,
        timeout: float | None = None,
        retry: bool = False,
    ) -> str:
        argv: list[str] = [self.vmrun_path, "-T", "ws"]
        if vm is not None:
            # Encrypted VMs need the encryption password to open the vmx. Kept out of logs.
            enc = vm.resolve_encryption_password()
            if enc:
                argv += ["-vp", enc]
        if guest_auth and vm is not None:
            argv += ["-gu", vm.guest.user, "-gp", vm.guest.resolve_password()]
        argv.append(command)
        if vm is not None:
            argv.append(vm.vmx)
        argv += list(args)
        attempts = self.retries if retry else 1
        last = ""
        for attempt in range(attempts):
            code, out = await self._runner(argv, timeout or self.default_timeout)
            if code == 0:
                log.debug("vmrun ok: %s", _mask_argv(argv))
                return out
            last = out.strip()
            log.warning("vmrun failed (rc=%s): %s -> %s", code, _mask_argv(argv), last[:200])
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (2**attempt))
        reason, hint = classify_vmrun_error(last)
        extra: dict[str, Any] = {"reason": reason} if reason else {}
        raise NtDriveError(
            BACKEND_ERROR,
            f"vmrun {command} failed: {last or 'exit code ' + str(code)}",
            hint,
            **extra,
        )

    async def health(self) -> dict[str, Any]:
        """Path, existence and version of vmrun."""
        exists = Path(self.vmrun_path).is_file()
        version = ""
        if exists:
            try:
                _, out = await self._runner([self.vmrun_path], 15.0)
                m = VERSION_RE.search(out)
                version = m.group(1) if m else out.strip().splitlines()[0][:80] if out else ""
            except (NtDriveError, OSError) as exc:
                version = f"error: {exc}"
        return {
            "backend": self.backend,
            "path": self.vmrun_path,
            "exists": exists,
            "version": version,
        }

    async def running_vmx_paths(self) -> list[str]:
        """Normalized paths of every running VM."""
        out = await self._exec("list", retry=True, timeout=30)
        paths: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("total running vms"):
                continue
            paths.append(_norm(line))
        return paths

    async def power_state(self, vm: VmConfig) -> PowerState:
        """Running if vmrun lists it, suspended if a .vmss exists, else off."""
        if _norm(vm.vmx) in await self.running_vmx_paths():
            return PowerState.RUNNING
        if Path(vm.vmx).with_suffix(".vmss").exists():
            return PowerState.SUSPENDED
        return PowerState.OFF

    async def start(self, vm: VmConfig, gui: bool = False) -> None:
        """`vmrun start` also resumes a suspended VM."""
        await self._exec("start", vm, "gui" if gui else "nogui", timeout=120)

    async def stop(self, vm: VmConfig, hard: bool = False) -> None:
        """Soft stop asks the guest to shut down (needs VMware Tools)."""
        await self._exec("stop", vm, "hard" if hard else "soft", timeout=180)

    async def reset(self, vm: VmConfig, hard: bool = True) -> None:
        """Hypervisor-side reset."""
        await self._exec("reset", vm, "hard" if hard else "soft", timeout=120)

    async def suspend(self, vm: VmConfig) -> None:
        """Suspend to disk."""
        await self._exec("suspend", vm, "hard", timeout=180)

    async def snapshot_take(self, vm: VmConfig, name: str) -> None:
        """Snapshot; vmrun includes memory when the VM is running."""
        await self._exec("snapshot", vm, name, timeout=600)

    async def snapshot_list(self, vm: VmConfig) -> SnapshotTree:
        """Tree from `listSnapshots showTree` plus the current node from the .vmsd file."""
        out = await self._exec("listSnapshots", vm, "showTree", retry=True, timeout=30)
        tree = SnapshotTree(roots=parse_snapshot_tree(out))
        vmsd = Path(vm.vmx).with_suffix(".vmsd")
        if vmsd.exists():
            try:
                tree.current = parse_current_snapshot(
                    vmsd.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                tree.current = None
        return tree

    async def snapshot_revert(self, vm: VmConfig, name: str) -> None:
        """Revert. vmrun leaves the VM stopped or suspended afterwards; callers call start()."""
        await self._exec("revertToSnapshot", vm, name, timeout=600)

    async def snapshot_delete(self, vm: VmConfig, name: str, children: bool = False) -> None:
        """Delete a snapshot and optionally its subtree."""
        args = [name] + (["andDeleteChildren"] if children else [])
        await self._exec("deleteSnapshot", vm, *args, timeout=600)

    async def guest_ip(self, vm: VmConfig, timeout: float = 60.0) -> str:
        """IPv4 from VMware Tools. `-wait` blocks until Tools report an address."""
        out = await self._exec("getGuestIPAddress", vm, "-wait", retry=True, timeout=timeout)
        ip = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            raise NtDriveError(BACKEND_ERROR, f"vmrun returned no guest IP: {out.strip()[:200]}")
        return ip

    async def screenshot(self, vm: VmConfig, out_path: str) -> str:
        """`captureScreen` needs VMware Tools and guest credentials."""
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        await self._exec("captureScreen", vm, out_path, guest_auth=True, timeout=60)
        return out_path

    async def copy_to_guest(self, vm: VmConfig, local: str, remote: str) -> None:
        """Guest tools file copy, used when SFTP is unavailable."""
        await self._exec("copyFileFromHostToGuest", vm, local, remote, guest_auth=True, timeout=600)

    async def copy_from_guest(self, vm: VmConfig, remote: str, local: str) -> None:
        """Guest tools file copy, used when SFTP is unavailable."""
        await self._exec("copyFileFromGuestToHost", vm, remote, local, guest_auth=True, timeout=600)

    async def run_in_guest(self, vm: VmConfig, program: str, args: list[str]) -> None:
        """Start a program in the guest without waiting for it."""
        await self._exec(
            "runProgramInGuest", vm, "-noWait", program, *args, guest_auth=True, timeout=60
        )

    async def ensure_serial_pipe(self, vm: VmConfig, pipe: str) -> bool:
        """Add a host named-pipe serial port to the vmx for serial KD. Returns True if changed.

        The VM must be powered off because Workstation rewrites the vmx on power off and would
        drop the edit. VMware is the pipe server and kd.exe connects as the client, which needs
        no network and no firewall. Idempotent: an identical serial0 block is left alone.
        """
        if not pipe.isascii():
            raise NtDriveError(
                INVALID_ARGS,
                f"serial pipe name must be ASCII: {pipe}",
                "set serial_pipe in vms.yaml",
            )
        power = await self.power_state(vm)
        if power != PowerState.OFF:
            raise NtDriveError(
                VM_NOT_RUNNING,
                f"VM {vm.name} is {power}; the vmx can only be edited while powered off",
                "call vm_stop (or vm_resume then vm_stop) and retry",
            )
        path = Path(vm.vmx)
        # vmx files are usually windows-1252. latin-1 maps every byte to one code point and back,
        # so the existing content survives unchanged and the ASCII lines added here are valid in
        # any of the encodings a vmx may declare.
        text = path.read_text(encoding="latin-1")
        wanted = {
            "serial0.present": "TRUE",
            "serial0.fileType": "pipe",
            "serial0.fileName": pipe,
            "serial0.pipe.endPoint": "server",
            "serial0.tryNoRxLoss": "TRUE",
            "serial0.yieldOnMsrRead": "TRUE",
            "serial0.startConnected": "TRUE",
        }
        current: dict[str, str] = {}
        kept: list[str] = []
        for line in text.splitlines():
            m = re.match(r'\s*(serial0\.[\w.]+)\s*=\s*"(.*)"\s*$', line)
            if m:
                current[m.group(1)] = m.group(2)
            elif not line.startswith("serial0."):
                kept.append(line)
        if current == wanted:
            return False
        block = [f'{key} = "{value}"' for key, value in wanted.items()]
        path.write_text("\n".join(kept + block) + "\n", encoding="latin-1")
        return True

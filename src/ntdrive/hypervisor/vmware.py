"""VMware Workstation adapter built on vmrun.exe.

vmrun is the only backend path: the Workstation REST API has no snapshot support. Every call is
`vmrun -T ws [-gu user -gp pass] <command> <vmx> [args]`. Read-only commands are retried because
vmrun fails intermittently when the Workstation UI process is busy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import psutil

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
from ntdrive.hostproc import run_hidden
from ntdrive.hypervisor.base import HypervisorAdapter, SnapshotNode, SnapshotTree
from ntdrive.hypervisor.vmx import apply_hardware, hardware_from_settings, vmx_settings

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


# vmrun goes through the shared hidden-window runner (ntdrive.hostproc). The name stays for
# the adapter's `runner` parameter and the tests.
subprocess_runner = run_hidden


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


KILL_HINT = (
    "vmrun is not answering for this VM. If the guest crashed or hung, vm_stop mode=kill "
    "confirm=true ends its vmware-vmx process on the host and clears the lock files, then "
    "vm_start boots it again"
)
# Host processes that belong to one VM: the VM itself and vmrun calls still working on it.
VMX_PROCESS_NAMES = {"vmware-vmx.exe", "vmware-vmx-debug.exe", "vmware-vmx-stats.exe", "vmrun.exe"}


def find_vmx_processes(vmx: str) -> list[Any]:
    """Psutil processes (vmware-vmx and vmrun) whose command line names this vmx."""
    target = _norm(vmx)
    found: list[Any] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        info = proc.info
        if (info.get("name") or "").lower() not in VMX_PROCESS_NAMES:
            continue
        if any(_norm(arg) == target for arg in (info.get("cmdline") or []) if arg):
            found.append(proc)
    return found


def remove_vmx_locks(vmx: str) -> list[str]:
    """Delete the *.lck directories and files Workstation leaves next to a killed VM.

    They make the next start fail with "appears to be in use". Only called once the VM's
    processes are gone. Returns the names removed.
    """
    removed: list[str] = []
    for entry in Path(vmx).parent.glob("*.lck"):
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            log.warning("could not remove %s: %s", entry, exc)
            continue
        removed.append(entry.name)
    return sorted(removed)


POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def parse_guest_stat(line: str) -> dict[str, Any] | None:
    """One `size|mtime|is_dir` line from guest_stat into a dict, or None when empty."""
    if not line:
        return None
    size, _, rest = line.partition("|")
    mtime, _, is_dir = rest.partition("|")
    try:
        return {
            "size": int(size),
            "modified": mtime or None,
            "is_dir": is_dir.strip().lower() == "true",
        }
    except ValueError:
        return None


def parse_guest_entry(line: str) -> dict[str, Any] | None:
    """One `name|size|mtime|is_dir` line from guest_list into a dict."""
    parts = line.split("|")
    if len(parts) != 4:
        return None
    name, size, mtime, is_dir = parts
    try:
        return {
            "name": name,
            "size": int(size),
            "modified": mtime or None,
            "is_dir": is_dir.strip().lower() == "true",
        }
    except ValueError:
        return None


def _norm_guest(path: str) -> str:
    """A Windows guest path in one spelling, for matching Get-FileHash output to what we sent."""
    return path.replace("/", "\\").rstrip("\\").lower()


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
            try:
                code, out = await self._runner(argv, timeout or self.default_timeout)
            except NtDriveError as exc:
                if exc.code != TIMEOUT:
                    raise
                # vmrun hangs when the VM's vmware-vmx process is wedged (a bugcheck under load
                # does it). The runner already killed this vmrun; say how to end the VM itself.
                hint = KILL_HINT if vm is not None else "check that VMware Workstation answers"
                raise NtDriveError(TIMEOUT, f"vmrun {command}: {exc.message}", hint) from None
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
        await self._require_off(vm)
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

    async def _require_off(self, vm: VmConfig) -> None:
        """Workstation rewrites the vmx on power off, so an edit only sticks while the VM is off."""
        power = await self.power_state(vm)
        if power != PowerState.OFF:
            raise NtDriveError(
                VM_NOT_RUNNING,
                f"VM {vm.name} is {power}; the vmx can only be edited while powered off",
                "call vm_stop (or vm_resume then vm_stop) and retry",
            )

    async def hardware(self, vm: VmConfig) -> dict[str, Any]:
        """cpus, cores_per_socket, memory_mb and nic from the vmx (readable at any power state)."""
        return hardware_from_settings(vmx_settings(vm.vmx))

    async def set_hardware(self, vm: VmConfig, changes: dict[str, Any]) -> dict[str, Any]:
        """Write cpus, memory_mb and nic into the vmx of a powered-off VM.

        The file is read and written as latin-1 so every other byte survives unchanged (see
        ensure_serial_pipe). Idempotent: values already in place count as no change.
        """
        await self._require_off(vm)
        path = Path(vm.vmx)
        text, changed = apply_hardware(path.read_text(encoding="latin-1"), changes)
        if changed:
            path.write_text(text, encoding="latin-1")
        return {"changed": changed, "hardware": await self.hardware(vm)}

    async def kill(self, vm: VmConfig) -> dict[str, Any]:
        """End the VM's vmware-vmx process on the host and clear its stale lock files.

        For a VM that vmrun no longer controls: after a guest bugcheck under load `vmrun stop
        hard` and `vmrun reset` time out again and again. Killing the process is a power cut,
        so the caller confirms it. vmrun processes still working on this vmx are ended too.
        """
        procs = find_vmx_processes(vm.vmx)
        killed: list[int] = []
        for proc in procs:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
                killed.append(proc.pid)
        for proc in procs:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.TimeoutExpired):
                await asyncio.to_thread(proc.wait, 10)
        removed = remove_vmx_locks(vm.vmx)
        log.warning("killed %s for %s and removed locks %s", killed, vm.name, removed)
        return {"killed": killed, "locks_removed": removed}

    async def guest_capture(self, vm: VmConfig, produce: str, timeout: float = 120.0) -> str:
        """Run a PowerShell pipeline in the guest and return its text output.

        `produce` is a pipeline whose output lines are captured. The lines are written to a temp
        file in the guest, copied to the host and deleted, so this works with only VMware Tools
        (no SSH). Used for the guest-tools fallback of the file query and delete tools and for
        hashing guest-tools copies.
        """
        report = rf"C:\Windows\Temp\ntdrive-{secrets.token_hex(6)}.txt"
        script = f"{produce} | Set-Content -Encoding UTF8 -LiteralPath '{report}'"
        await self._exec(
            "runProgramInGuest",
            vm,
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            guest_auth=True,
            timeout=timeout,
        )
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "out.txt")
            await self._exec(
                "copyFileFromGuestToHost", vm, report, local, guest_auth=True, timeout=120
            )
            text = Path(local).read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        with contextlib.suppress(NtDriveError):
            await self._exec("deleteFileInGuest", vm, report, guest_auth=True, timeout=60)
        return text

    async def guest_sha256(self, vm: VmConfig, remotes: list[str]) -> dict[str, str]:
        """Get-FileHash in the guest through VMware Tools, one run for all files.

        Verifies guest-tools copies the way SFTP copies are verified. Files PowerShell could not
        hash are simply absent from the result.
        """
        if not remotes:
            return {}
        quoted = ",".join("'" + remote.replace("'", "''") + "'" for remote in remotes)
        produce = (
            f"Get-FileHash -LiteralPath {quoted} -Algorithm SHA256 -ErrorAction SilentlyContinue"
            " | ForEach-Object { $_.Hash + ' ' + $_.Path }"
        )
        text = await self.guest_capture(vm, produce, timeout=300)
        by_path: dict[str, str] = {}
        for line in text.splitlines():
            digest, _, path = line.strip().partition(" ")
            if digest and path:
                by_path[_norm_guest(path)] = digest.lower()
        return {r: by_path[_norm_guest(r)] for r in remotes if _norm_guest(r) in by_path}

    async def guest_stat(self, vm: VmConfig, remote: str) -> dict[str, Any] | None:
        """size, modified and is_dir from Get-Item in the guest, or None when absent."""
        lit = remote.replace("'", "''")
        # `|` is illegal in a Windows path, so it is a safe field delimiter.
        produce = (
            f"$i = Get-Item -LiteralPath '{lit}' -Force -ErrorAction SilentlyContinue; "
            "if ($i) { '{0}|{1}|{2}' -f [long]$i.Length, "
            "$i.LastWriteTimeUtc.ToString('o'), [bool]$i.PSIsContainer }"
        )
        line = (await self.guest_capture(vm, produce)).strip()
        return parse_guest_stat(line)

    async def guest_list(self, vm: VmConfig, remote: str) -> list[dict[str, Any]] | None:
        """Directory entries from Get-ChildItem in the guest, or None when the path is absent."""
        lit = remote.replace("'", "''")
        produce = (
            f"if (Test-Path -LiteralPath '{lit}') {{ Get-ChildItem -LiteralPath '{lit}' -Force"
            " -ErrorAction SilentlyContinue | ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.Name, "
            "[long]$_.Length, $_.LastWriteTimeUtc.ToString('o'), [bool]$_.PSIsContainer } }"
            " else { 'ntdrive:absent' }"
        )
        text = await self.guest_capture(vm, produce)
        if text.strip() == "ntdrive:absent":
            return None
        entries: list[dict[str, Any]] = []
        for line in text.splitlines():
            parsed = parse_guest_entry(line)
            if parsed is not None:
                entries.append(parsed)
        return entries

    async def guest_delete(self, vm: VmConfig, remote: str, recurse: bool = False) -> bool:
        """Remove-Item in the guest. False when the path was already absent."""
        lit = remote.replace("'", "''")
        rec = " -Recurse" if recurse else ""
        check_failed = f"if (Test-Path -LiteralPath '{lit}') {{ 'ntdrive:failed' }}"
        produce = (
            f"if (Test-Path -LiteralPath '{lit}') {{ "
            f"Remove-Item -LiteralPath '{lit}' -Force{rec} -ErrorAction SilentlyContinue; "
            f"{check_failed} else {{ 'ntdrive:deleted' }} }} else {{ 'ntdrive:absent' }}"
        )
        result = (await self.guest_capture(vm, produce)).strip()
        if result == "ntdrive:failed":
            raise NtDriveError(
                BACKEND_ERROR,
                f"could not delete {remote} in the guest",
                "it may be in use or need a recurse for a non-empty directory",
            )
        return result == "ntdrive:deleted"

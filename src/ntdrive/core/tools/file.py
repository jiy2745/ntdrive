"""file_*: host <-> guest file transfer (SFTP first, guest tools as fallback)."""

from __future__ import annotations

import asyncio
import glob
import hashlib
import os
from pathlib import Path
from typing import Any

from pydantic import Field

from ntdrive.core.registry import tool
from ntdrive.core.service import NtDriveService
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import INVALID_ARGS, NtDriveError
from ntdrive.paths import is_absolute_local

# Exceptions that mean "SFTP did not work for this file, use guest tools instead".
_SFTP_FAILURES = (NotImplementedError, NtDriveError, OSError)


class StatParams(VmParams):
    """file_stat."""

    remote: str = Field(description="Guest path to inspect")


class LsParams(VmParams):
    """file_ls."""

    remote: str = Field(description="Guest directory to list")


class DeleteParams(VmParams):
    """file_delete."""

    remote: str = Field(description="Guest file or directory to delete")
    recurse: bool = Field(
        default=False, description="Delete a non-empty directory and its contents"
    )


class PushParams(VmParams):
    """file_push."""

    local: str = Field(description="Host file, directory or glob (absolute path)")
    remote: str = Field(description="Guest path; a directory when local is several files")
    verify: bool = Field(default=True, description="Compare SHA-256 after the copy")


class PullParams(VmParams):
    """file_pull."""

    remote: str = Field(description="Guest file path")
    local: str = Field(
        description="Host file, or a directory when it ends with a separator (absolute path)"
    )


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _sha256_async(path: str) -> str:
    # Hashing a large driver package must not stall the event loop that serves terminals.
    return await asyncio.get_running_loop().run_in_executor(None, _sha256, path)


def _expand_local(pattern: str) -> list[tuple[str, str]]:
    """(absolute path, relative name) for every file matched by a path, dir or glob."""
    path = Path(pattern)
    if path.is_dir():
        files = [p for p in path.rglob("*") if p.is_file()]
        return [(str(p), str(p.relative_to(path))) for p in files]
    if path.is_file():
        return [(str(path), path.name)]
    matches = [Path(m) for m in glob.glob(pattern, recursive=True)]
    return [(str(m), m.name) for m in matches if m.is_file()]


def _join_remote(base: str, rel: str) -> str:
    sep = "\\" if ("\\" in base or ":" in base) else "/"
    return base.rstrip("\\/") + sep + rel.replace("/", sep)


def _require_absolute(local: str) -> None:
    """The daemon has its own working directory, so a relative host path is a mistake."""
    if not is_absolute_local(local):
        raise NtDriveError(
            INVALID_ARGS,
            f"local must be an absolute host path, got {local!r}",
            "the daemon runs in another process with a different working directory; the CLI "
            "and SDK absolutize for you, MCP and HTTP callers must pass a full path",
        )


@tool(
    "file_push",
    "Copy a file, directory or glob from the host into the guest and verify it by SHA-256 "
    "(over SFTP, or through VMware Tools when SSH is down).",
    PushParams,
    positional=("vm", "local", "remote"),
    touches_guest=True,
    effect="destructive",
    idempotent=True,
)
async def file_push(service: NtDriveService, p: PushParams) -> dict[str, Any]:
    """Upload."""
    cfg = service.vm_cfg(p.vm)
    service.ensure_not_frozen(p.vm)
    _require_absolute(p.local)
    await service.ensure_running(cfg)
    files = [(os.path.abspath(local), rel) for local, rel in _expand_local(p.local)]
    if not files:
        raise NtDriveError(INVALID_ARGS, f"nothing matches {p.local}")
    many = len(files) > 1 or Path(p.local).is_dir()
    remote_is_dir = many or p.remote.endswith(("\\", "/"))
    transport = await service.file_transport(cfg)
    adapter = service.adapter_for(cfg)
    total = 0
    verified = 0
    hash_failures = 0
    via = transport.name if transport is not None else "guest_tools"
    notes: list[str] = []
    if transport is None:
        notes.append("sftp unavailable (no ssh), copied with guest tools")
    copied: list[dict[str, Any]] = []
    # Guest-tools copies are hashed afterwards in one batch (Get-FileHash in the guest).
    tools_copied: list[tuple[dict[str, Any], str, str]] = []
    for local, rel in files:
        remote = _join_remote(p.remote, rel) if remote_is_dir else p.remote
        size: int | None = None
        if transport is not None:
            try:
                size = await transport.put_file(local, remote)
            except _SFTP_FAILURES as exc:
                # SFTP broke mid-way. Stop trying it for the remaining files.
                transport = None
                via = "guest_tools"
                notes.append(f"sftp failed ({exc}), remaining files copied with guest tools")
        by_tools = False
        if size is None:
            await adapter.copy_to_guest(cfg, local, remote)
            size = os.path.getsize(local)
            by_tools = True
        entry: dict[str, Any] = {"local": local, "remote": remote, "bytes": size}
        if by_tools and p.verify:
            tools_copied.append((entry, local, remote))
        ok: bool | None = None
        if p.verify and transport is not None:
            try:
                remote_hash = await transport.remote_sha256(remote)
            except _SFTP_FAILURES as exc:
                remote_hash = None
                entry["verify_error"] = str(exc)
                hash_failures += 1
            if remote_hash is not None:
                ok = remote_hash == await _sha256_async(local)
                verified += 1 if ok else 0
        entry["verified"] = ok
        total += size
        copied.append(entry)
    if tools_copied:
        try:
            hashes = await adapter.guest_sha256(cfg, [remote for _, _, remote in tools_copied])
        except NtDriveError as exc:
            hashes = {}
            for entry, _, _ in tools_copied:
                entry["verify_error"] = f"guest tools could not hash it: {exc.message}"
            hash_failures += len(tools_copied)
        else:
            for entry, local, remote in tools_copied:
                digest = hashes.get(remote)
                if digest is None:
                    entry["verify_error"] = "guest tools could not hash it"
                    hash_failures += 1
                    continue
                entry["verified"] = digest == await _sha256_async(local)
                verified += 1 if entry["verified"] else 0
    if hash_failures:
        notes.append(
            f"sha256 verification failed for {hash_failures} file(s), see copied[].verify_error"
        )
    result: dict[str, Any] = {
        "vm": p.vm,
        "files": len(files),
        "bytes": total,
        "verified": verified,
        "via": via,
        "copied": copied,
    }
    if notes:
        result["note"] = "; ".join(notes)
    return result


@tool(
    "file_pull",
    "Copy a file from the guest to the host.",
    PullParams,
    positional=("vm", "remote", "local"),
    touches_guest=True,
    effect="additive",
    idempotent=True,
)
async def file_pull(service: NtDriveService, p: PullParams) -> dict[str, Any]:
    """Download."""
    cfg = service.vm_cfg(p.vm)
    service.ensure_not_frozen(p.vm)
    _require_absolute(p.local)
    await service.ensure_running(cfg)
    local = os.path.abspath(p.local)
    if os.path.isdir(local) or p.local.endswith(("\\", "/")):
        os.makedirs(local, exist_ok=True)
        local = os.path.join(local, p.remote.replace("\\", "/").rsplit("/", 1)[-1])
    transport = await service.file_transport(cfg)
    size: int | None = None
    via = "guest_tools"
    note = ""
    if transport is not None:
        try:
            size = await transport.get_file(p.remote, local)
            via = transport.name
        except _SFTP_FAILURES as exc:
            size = None
            note = f"sftp failed ({exc}), copied with guest tools"
    if size is None:
        try:
            await service.adapter_for(cfg).copy_from_guest(cfg, p.remote, local)
        except NtDriveError as exc:
            if transport is not None:
                raise
            # Neither SSH nor VMware Tools answered: a crashed, frozen or booting guest. The
            # debugger still works on a crashed one, and can write a dump on the host.
            raise NtDriveError(
                exc.code,
                exc.message,
                "the guest answers neither SSH nor VMware Tools (crashed, frozen or booting). "
                "With the debugger attached, kd_wait_event and kd_exec '!analyze -v' work "
                "without the guest and '.dump /f <host path>' saves a crash dump on the host. "
                "Otherwise reboot the guest and pull the file afterwards",
                **exc.extra,
            ) from None
        size = os.path.getsize(local)
    result: dict[str, Any] = {
        "vm": p.vm,
        "local": local,
        "remote": p.remote,
        "bytes": size,
        "via": via,
    }
    if note:
        result["note"] = note
    return result


@tool(
    "file_stat",
    "Size, last-modified time and is_dir of a guest path, so freshness can be checked without a "
    "shell. exists=false when the path is not there.",
    StatParams,
    positional=("vm", "remote"),
    touches_guest=True,
    effect="read",
)
async def file_stat(service: NtDriveService, p: StatParams) -> dict[str, Any]:
    """Stat a guest path over SFTP, falling back to VMware Tools."""
    cfg = service.vm_cfg(p.vm)
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    transport = await service.file_transport(cfg)
    info: dict[str, Any] | None = None
    via = "guest_tools"
    if transport is not None:
        try:
            info = await transport.stat_file(p.remote)
            via = transport.name
        except _SFTP_FAILURES:
            transport = None
    if transport is None:
        info = await service.adapter_for(cfg).guest_stat(cfg, p.remote)
    result: dict[str, Any] = {
        "vm": p.vm,
        "remote": p.remote,
        "exists": info is not None,
        "via": via,
    }
    if info is not None:
        result.update(info)
    return result


@tool(
    "file_ls",
    "List a guest directory (each entry name, size, modified, is_dir), without a shell.",
    LsParams,
    positional=("vm", "remote"),
    touches_guest=True,
    effect="read",
)
async def file_ls(service: NtDriveService, p: LsParams) -> dict[str, Any]:
    """List a guest directory over SFTP, falling back to VMware Tools."""
    cfg = service.vm_cfg(p.vm)
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    transport = await service.file_transport(cfg)
    entries: list[dict[str, Any]] | None = None
    via = "guest_tools"
    if transport is not None:
        try:
            entries = await transport.list_dir(p.remote)
            via = transport.name
        except _SFTP_FAILURES:
            transport = None
    if transport is None:
        entries = await service.adapter_for(cfg).guest_list(cfg, p.remote)
    return {
        "vm": p.vm,
        "remote": p.remote,
        "exists": entries is not None,
        "entries": entries or [],
        "via": via,
    }


@tool(
    "file_delete",
    "Delete a guest file or directory (recurse for a non-empty directory). deleted=false when it "
    "was already absent.",
    DeleteParams,
    positional=("vm", "remote"),
    touches_guest=True,
    effect="destructive",
)
async def file_delete(service: NtDriveService, p: DeleteParams) -> dict[str, Any]:
    """Delete a guest path over SFTP, falling back to VMware Tools."""
    cfg = service.vm_cfg(p.vm)
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    transport = await service.file_transport(cfg)
    deleted: bool
    via = "guest_tools"
    if transport is not None:
        try:
            await transport.delete_file(p.remote, p.recurse)
            deleted, via = True, transport.name
        except FileNotFoundError:
            deleted, via = False, transport.name
        except _SFTP_FAILURES:
            transport = None
    if transport is None:
        deleted = await service.adapter_for(cfg).guest_delete(cfg, p.remote, p.recurse)
    if deleted:
        service.state.record_event(p.vm, "file_delete", remote=p.remote)
    return {"vm": p.vm, "remote": p.remote, "deleted": deleted, "via": via}

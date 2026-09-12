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
    "Copy a file, directory or glob from the host into the guest and verify it.",
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
        if size is None:
            await adapter.copy_to_guest(cfg, local, remote)
            size = os.path.getsize(local)
        entry: dict[str, Any] = {"local": local, "remote": remote, "bytes": size}
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
        await service.adapter_for(cfg).copy_from_guest(cfg, p.remote, local)
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

"""file_*: host <-> guest file transfer (SFTP first, guest tools as fallback)."""

from __future__ import annotations

import asyncio
import base64
import glob
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ntdrive.core.registry import tool
from ntdrive.core.tools.common import VmParams
from ntdrive.errors import INVALID_ARGS, VERIFY_FAILED, NtDriveError
from ntdrive.paths import is_absolute_local

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

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
    require_verified: bool = Field(
        default=False,
        description=(
            "Also fail when a file could not be hashed at all, not only on a mismatch. A "
            "mismatch always fails; this turns 'unproven' into a failure too"
        ),
    )
    flush: bool = Field(
        default=True,
        description=(
            "Flush each copied file to the guest's disk (FlushFileBuffers) before verifying, so a "
            "bugcheck right after the push cannot lose the write and leave the old file in place. "
            "Needs SSH; costs one guest command per push"
        ),
    )
    grant_users_rx: bool = Field(
        default=False,
        description=(
            "After copying, grant BUILTIN\\Users read and execute on each file. Pushed files "
            "inherit admin-only ACLs, so a standard account cannot read or run them without this"
        ),
    )


class PullParams(VmParams):
    """file_pull."""

    remote: str = Field(description="Guest file path")
    local: str = Field(
        description="Host file, or a directory when it ends with a separator (absolute path)"
    )
    verify: bool = Field(
        default=True,
        description=(
            "Compare the size of the host copy against the guest file and fail on a mismatch, so "
            "a truncated pull cannot read as success"
        ),
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


# One guest command carries this many paths at most. The text is base64 of UTF-16LE inside a
# command line, so it inflates about 2.7x, and Windows stops reading a command line at 32767
# characters. 40 paths of ordinary length stay far below that.
_FLUSH_BATCH = 40


def _flush_script(remotes: list[str]) -> str:
    """PowerShell that opens each file for writing and calls FlushFileBuffers on it."""
    paths = ",".join("'" + remote.replace("'", "''") + "'" for remote in remotes)
    return (
        f"foreach ($p in @({paths})) {{ try {{ "
        "$f = [IO.File]::Open($p, 'Open', 'Write', 'ReadWrite'); $f.Flush($true); $f.Dispose() "
        "} catch { Write-Output ('fail ' + $p + ': ' + $_.Exception.Message) } }"
    )


async def _flush_to_disk(shell: Any, remotes: list[str]) -> list[str]:
    """Force the guest to write each pushed file to its disk. Returns problems, not raising.

    SFTP does not fsync: OpenSSH's sftp-server only syncs when the client asks through the
    `fsync@openssh.com` extension, and paramiko never asks. So a pushed file can live entirely in
    the guest's page cache. Verification then reads it straight back out of that same cache and
    matches, which makes `verified: true` honest about the running guest but not about its disk.

    On a target that is about to be bugchecked - which is most of this project's work - that gap
    is a lost write. A bugcheck is a power cut as far as the filesystem is concerned, and NTFS
    journals metadata, not file data, so after the reboot the *previous* build is back on disk
    while the push reported success. Flushing before verification closes the gap and makes
    `verified: true` mean "on the guest's disk", which is what a caller assumes it means.

    Failures here are reported, never raised: the copy itself succeeded, and a file that cannot be
    opened for writing (a driver that is already loaded, say) is a durability unknown, not a
    broken push.
    """
    problems: list[str] = []
    for start in range(0, len(remotes), _FLUSH_BATCH):
        batch = remotes[start : start + _FLUSH_BATCH]
        script = _flush_script(batch)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        # The guest's default SSH shell may be cmd.exe, so call PowerShell by name and hand it
        # the script as -EncodedCommand: no quoting layer can alter it on the way.
        command = f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"
        try:
            code, out = await shell.exec_once(command, timeout=60)
        except NtDriveError as exc:
            problems.append(exc.message)
            continue
        problems.extend(ln[5:].strip() for ln in out.splitlines() if ln.startswith("fail "))
        if code != 0 and not problems:
            problems.append(f"flush exited {code}: {out.strip()[:120]}")
    return problems


def _record_hashes(entry: dict[str, Any], local_hash: str, remote_hash: str) -> bool:
    """Write the digests into one copied[] entry and say whether they matched.

    On a match the single agreed digest is reported, so a caller can assert what is on the guest
    and compare it later against the guest's own Get-FileHash. A boolean can only be trusted, a
    digest can be checked. On a mismatch both sides are reported, because which one is wrong is
    the first thing anyone will ask.
    """
    ok = local_hash == remote_hash
    entry["verified"] = ok
    if ok:
        entry["sha256"] = local_hash
    else:
        entry["sha256_local"] = local_hash
        entry["sha256_remote"] = remote_hash
    return ok


@tool(
    "file_push",
    "Copy a file, directory or glob from the host into the guest, flush it to the guest's disk "
    "and verify it by SHA-256 (over SFTP, or through VMware Tools when SSH is down). Fails with "
    "verify_failed when the copy does not match, so a corrupt push cannot read as success.",
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
    if p.require_verified and not p.verify:
        raise NtDriveError(
            INVALID_ARGS,
            "require_verified=true needs verify=true",
            "with verification off there is nothing to require; drop one of the two",
        )
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
    # Copy first, then flush, then hash: verifying before the flush would only prove that the
    # guest's page cache holds the right bytes, which a bugcheck can undo. (entry, local, remote)
    # for each file, split by what carried it, because only SFTP copies can be hashed over SFTP.
    sftp_copied: list[tuple[dict[str, Any], str, str]] = []
    tools_copied: list[tuple[dict[str, Any], str, str]] = []
    for local, rel in files:
        remote = _join_remote(p.remote, rel) if remote_is_dir else p.remote
        entry: dict[str, Any] = {"local": local, "remote": remote}
        if p.verify and transport is not None:
            # One cheap stat before the copy, so "it was already this file" and "the thing that
            # was there had a different size" are both visible afterwards instead of invisible.
            # Guest tools have no cheap stat (every call is a vmrun round trip), so this is the
            # SFTP path only.
            try:
                before = await transport.stat_file(remote)
            except _SFTP_FAILURES:
                pass  # unknown, so say nothing rather than claim it was absent
            else:
                entry["replaced"] = before is not None
                if before is not None:
                    entry["replaced_bytes"] = before.get("size")
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
        entry["bytes"] = size
        entry["verified"] = None
        total += size
        copied.append(entry)
        (tools_copied if by_tools else sftp_copied).append((entry, local, remote))
    if p.flush and copied:
        try:
            shell = await service.transport(cfg)
            if not hasattr(shell, "exec_once"):
                raise NtDriveError(INVALID_ARGS, "the transport cannot run commands")
            problems = await _flush_to_disk(shell, [e["remote"] for e in copied])
        except NtDriveError as exc:
            result_flushed: bool | None = None
            notes.append(
                f"not flushed to the guest disk (needs SSH): {exc.message}. The copy is in the "
                "guest's page cache, so a bugcheck before Windows writes it back restores the "
                "previous file while this push reports success"
            )
        else:
            result_flushed = not problems
            if problems:
                notes.append(f"could not flush {len(problems)} file(s) to disk: {problems[0]}")
    else:
        result_flushed = None
    if p.verify:
        # Files SFTP carried are hashed over SFTP; everything else, including anything whose SFTP
        # hash failed, goes into one Get-FileHash run in the guest.
        hash_batch: list[tuple[dict[str, Any], str, str]] = list(tools_copied)
        for entry, local, remote in sftp_copied:
            remote_hash: str | None = None
            if transport is None:
                # The link died later in the copy loop, so this file cannot be hashed over SFTP.
                entry["verify_note"] = "sftp dropped after the copy; rehashed with guest tools"
            else:
                try:
                    remote_hash = await transport.remote_sha256(remote)
                except _SFTP_FAILURES as exc:
                    # The copy may be fine but the SFTP read failed, or the copy is genuinely
                    # corrupt. Either way, do not leave it unverified: rehash through guest tools,
                    # which decides. A silent verified:null once let a corrupt pushed exe read as
                    # merely unchecked.
                    entry["verify_note"] = f"sftp hash failed ({exc}); rehashed with guest tools"
            if remote_hash is None:
                hash_batch.append((entry, local, remote))
                continue
            verified += 1 if _record_hashes(entry, await _sha256_async(local), remote_hash) else 0
        if hash_batch:
            try:
                hashes = await adapter.guest_sha256(cfg, [remote for _, _, remote in hash_batch])
            except NtDriveError as exc:
                hashes = {}
                for entry, local, _ in hash_batch:
                    entry["verify_error"] = f"guest tools could not hash it: {exc.message}"
                    # Still report the host digest: the caller can check the guest themselves.
                    entry["sha256_local"] = await _sha256_async(local)
                hash_failures += len(hash_batch)
            else:
                for entry, local, remote in hash_batch:
                    digest = hashes.get(remote)
                    local_hash = await _sha256_async(local)
                    if digest is None:
                        entry["verify_error"] = "guest tools could not hash it"
                        entry["sha256_local"] = local_hash
                        hash_failures += 1
                        continue
                    verified += 1 if _record_hashes(entry, local_hash, digest) else 0
    if hash_failures:
        notes.append(
            f"sha256 verification failed for {hash_failures} file(s), see copied[].verify_error"
        )
    users_rx: list[str] = []
    if p.grant_users_rx and copied:
        # BUILTIN\Users is *S-1-5-32-545, language-independent. icacls needs a guest shell, so
        # this rides SSH; without it (guest-tools copy) the ACL cannot be set here.
        try:
            shell = await service.transport(cfg)
            if not hasattr(shell, "exec_once"):
                raise NtDriveError(INVALID_ARGS, "the transport cannot run commands")
            for entry in copied:
                target = entry["remote"]
                code, out = await shell.exec_once(
                    f'icacls "{target}" /grant "*S-1-5-32-545:(RX)"', timeout=30
                )
                if code == 0:
                    users_rx.append(target)
                else:
                    notes.append(f"could not grant Users:RX on {target}: {out.strip()[:120]}")
        except NtDriveError as exc:
            notes.append(f"could not grant Users:RX (needs SSH to the guest): {exc.message}")
    # Top-level `verified` is a bool (or null), the same type as each copied[].verified, so a
    # caller never has to tell "verified: 3" (a count) from "verified: true". It is true only when
    # every file was checked and matched, false on any mismatch or unreadable hash, null when
    # verification was off. `verified_count` keeps the number.
    # `false` means a real mismatch, never "the hash could not be read". Under guest load hashing
    # fails while the copy is fine, and reporting that as false read as data corruption.
    if not p.verify:
        overall: bool | None = None
    elif any(entry.get("verified") is False for entry in copied):
        overall = False
    elif copied and all(entry.get("verified") is True for entry in copied):
        overall = True
    else:
        overall = None  # some file could not be hashed: unknown, not failed
    result: dict[str, Any] = {
        "vm": p.vm,
        "files": len(files),
        "bytes": total,
        "verified": overall,
        "verified_count": verified,
        "via": via,
        "copied": copied,
    }
    if result_flushed is not None:
        result["flushed"] = result_flushed
    if p.grant_users_rx:
        result["users_rx"] = users_rx
    if notes:
        result["note"] = "; ".join(notes)
    # A verification whose answer nobody has to read is not a verification. Raising makes the one
    # failure that matters - the guest holds bytes other than the ones that were sent - reach every
    # front door: a non-zero CLI exit code, an MCP tool error, an HTTP 409. It used to be a field
    # in a long successful result, and a shell loop that did not parse JSON saw exit 0.
    if overall is False:
        bad = [str(e["remote"]) for e in copied if e.get("verified") is False]
        shown = ", ".join(bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else "")
        raise NtDriveError(
            VERIFY_FAILED,
            f"the copy on the guest does not match what was sent: {shown}",
            "the guest holds bytes other than the file that was built, so anything loaded from "
            "that path is the wrong binary. file_delete the path and push again. verify=false "
            "only silences the check, it does not change what is on the guest",
            result=result,
        )
    if p.require_verified and overall is None:
        raise NtDriveError(
            VERIFY_FAILED,
            f"the copy on the guest is unproven: {hash_failures} of {len(files)} file(s) could "
            "not be hashed back",
            "require_verified=true treats an unproven copy as a failure. The copy itself "
            "succeeded; the guest could not hash it back, which usually means it is under load, "
            "rebooting, or frozen by the debugger (kd_go first). Retry, or drop require_verified "
            "to accept an unproven copy",
            result=result,
        )
    return result


@tool(
    "file_pull",
    "Copy a file from the guest to the host and check its size, failing with verify_failed when "
    "the host copy is short.",
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
    # A pull had no check of any kind: a transfer that stopped early landed a short file on the
    # host and said nothing. Comparing the guest's size costs one stat and catches that. It is a
    # weaker check than file_push's digest, so the result says which check ran.
    if p.verify and transport is not None and via == transport.name:
        remote_size: int | None = None
        try:
            info = await transport.stat_file(p.remote)
        except _SFTP_FAILURES:
            info = None
        if info is not None:
            remote_size = info.get("size")
        if remote_size is None:
            result["verified"] = None
        else:
            result["remote_bytes"] = remote_size
            result["verified"] = remote_size == size
            result["verified_by"] = "size"
        if result["verified"] is False:
            raise NtDriveError(
                VERIFY_FAILED,
                f"the host copy is {size} bytes, the guest file is {remote_size}",
                "the pull did not bring the whole file. The short copy is on the host at the "
                "path in result.local; pull it again. A file the guest is still writing (a live "
                "log, a dump being saved) changes size mid-copy and mismatches for that reason",
                result=result,
            )
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

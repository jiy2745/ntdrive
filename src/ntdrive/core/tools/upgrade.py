"""vm_upgrade_*: upgrade a guest in place to a chosen Windows Insider build.

The pipeline, and why each step is the way it is (every reason is a failure seen live):

1. The host asks the UUP dump JSON API (api.uupdump.net) which files make up the build. Only the
   metadata comes from there. Every file URL must be on a microsoft.com host, and each file is
   checked against its SHA-1 after download. The uupdump conversion pack (its aria2c, 7-Zip and
   converter scripts) is never fetched or run.
2. The guest downloads the files itself (curl.exe against the Microsoft CDN). Pushing them over
   SFTP ran at about 1.75 MB/s, so a 4 GB build would take forty minutes. The guest's own
   download took three minutes for 2.6 GB.
3. The setup media tree comes from index 1 of the metadata ESD (DISM). The install image (index 3)
   keeps its file data in the other package ESDs and, for in-box drivers, in the FoD CABs. DISM
   cannot resolve those cross-file references (error 0x80070714 even with /SWMFile), so wimlib
   merges them: each CAB is expanded with expand.exe and captured as a reference WIM, then the
   install image and WinRE are exported with every ESD and reference WIM as --ref, and WinRE is
   put back into the install image so the upgraded guest keeps a recovery environment.
4. Everything runs in the guest as SYSTEM from an on-demand scheduled task. A process started
   from an SSH session dies with the session, and setup outlives several reboots.
5. Kernel debugging is switched off before setup.exe runs. Booting the setup environment with
   KDNET active can NMI the guest, and setup rewrites the boot entry anyway, so kd_setup_guest is
   part of finishing the upgrade.

wimlib (https://wimlib.net) is fetched once on the host into the ntdrive state directory and
checked against a pinned SHA-256 before it is used.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

from pydantic import BaseModel, Field

from ntdrive.config import state_dir
from ntdrive.core.registry import tool
from ntdrive.core.state import KdState
from ntdrive.core.tools.common import ConfirmMixin, VmParams
from ntdrive.errors import BACKEND_ERROR, INVALID_ARGS, KD_ALREADY_ATTACHED, NtDriveError

if TYPE_CHECKING:
    from ntdrive.core.service import NtDriveService

UUP_API = "https://api.uupdump.net"
WIMLIB_VERSION = "1.14.5"
WIMLIB_ZIP = f"wimlib-{WIMLIB_VERSION}-windows-x86_64-bin.zip"
WIMLIB_URL = f"https://wimlib.net/downloads/{WIMLIB_ZIP}"
# Pinned: the zip downloaded from wimlib.net on 2026-11-08 (released 2026-01-30, 779 KB).
WIMLIB_SHA256 = "2f446d6fa3866582175f1a22a7be198eeee0aec7aba5b4e04ad25c99eae2d265"

GUEST_ROOT = "C:\\ntdrive-upgrade"
# Peak use while building is about 10 GB (files, reference images, install.wim, WinRE), and
# setup then wants about 20 GB of its own. The pipeline checks the second number again itself.
MIN_FREE_GB = 30.0
MIN_FREE_SETUP_GB = 20.0

_BUILD_ARG = re.compile(r"^(latest|\d{5}(\.\d{1,5})?)$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_LANG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")
_INSIDER_TITLE = "Windows 11 Insider Preview"
_BUILDING = {
    "windows_old",
    "download",
    "tools",
    "media",
    "refs",
    "install_image",
    "winre",
    "cleanup",
}


# -- host-side HTTP, kept as module functions so tests can replace them ----------------------


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "ntdrive"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ntdrive"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data: bytes = response.read()
        return data


async def _uup(endpoint: str, **params: str) -> dict[str, Any]:
    url = f"{UUP_API}/{endpoint}?{urlencode(params)}"
    try:
        data = await asyncio.to_thread(_fetch_json, url)
    except Exception as exc:  # network, HTTP or JSON failure, all the same to the caller
        raise NtDriveError(
            BACKEND_ERROR,
            f"the UUP dump API did not answer {endpoint}: {exc}",
            "api.uupdump.net is a third-party service, check the host's network and retry later",
        ) from exc
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, dict) or response.get("error"):
        detail = response.get("error") if isinstance(response, dict) else "no response object"
        raise NtDriveError(BACKEND_ERROR, f"the UUP dump API refused {endpoint}: {detail}")
    return response


async def _wimlib_zip() -> str:
    """Path to the pinned wimlib zip on the host, downloading it once."""
    cache = state_dir() / "cache"
    path = cache / WIMLIB_ZIP
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == WIMLIB_SHA256:
        return str(path)
    try:
        data = await asyncio.to_thread(_fetch_bytes, WIMLIB_URL)
    except Exception as exc:
        raise NtDriveError(
            BACKEND_ERROR,
            f"could not download {WIMLIB_URL}: {exc}",
            f"download it by hand and save it as {path}",
        ) from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != WIMLIB_SHA256:
        raise NtDriveError(
            BACKEND_ERROR,
            f"{WIMLIB_ZIP} from wimlib.net has sha256 {digest}, expected {WIMLIB_SHA256}",
            "the file on the site changed, so it is not used. Check wimlib.net before updating "
            "the pinned digest in ntdrive.core.tools.upgrade",
        )
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _version_key(build: str) -> tuple[int, ...]:
    return tuple(int(part) for part in build.split(".") if part.isdigit())


async def _insider_builds(search: str) -> list[dict[str, Any]]:
    response = await _uup("listid.php", search=search, sortByDate="1")
    raw = response.get("builds") or {}
    items = raw.values() if isinstance(raw, dict) else raw
    builds = [
        {
            "build": str(b.get("build", "")),
            "uuid": str(b.get("uuid", "")),
            "title": str(b.get("title", "")),
            "created": b.get("created"),
        }
        for b in items
        if isinstance(b, dict)
        and b.get("arch") == "amd64"
        and str(b.get("title", "")).startswith(_INSIDER_TITLE)
        and "Update" not in str(b.get("title", ""))
        and _BUILD_ARG.match(str(b.get("build", "")))
    ]
    builds.sort(key=lambda b: _version_key(str(b["build"])), reverse=True)
    return builds


# -- guest scripts ---------------------------------------------------------------------------


def _encoded(script: str) -> str:
    """A PowerShell command line that survives any default SSH shell (see file.py's flush)."""
    blob = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {blob}"


PROBE_PS = r"""
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$lcid = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\Language').InstallLanguage
'BUILD=' + $cv.CurrentBuild
'UBR=' + $cv.UBR
'EDITION=' + $cv.EditionID
'LANG=' + [Globalization.CultureInfo]::GetCultureInfo([Convert]::ToInt32($lcid, 16)).Name.ToLower()
'FREE_GB=' + [math]::Round((Get-PSDrive C).Free / 1GB, 1)
'CPUS=' + (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
'WINDOWS_OLD=' + (Test-Path 'C:\Windows.old')
$t = Get-ScheduledTask -TaskName 'ntdrive-upgrade' -ErrorAction SilentlyContinue
'TASK=' + $(if ($t) { [string]$t.State } else { 'none' })
'SETUP_RUNNING=' + [bool](Get-Process SetupHost -ErrorAction SilentlyContinue)
$s = 'C:\ntdrive-upgrade\state.json'
'STATE=' + $(if (Test-Path $s) { (Get-Content $s -Raw).Trim() } else { '' })
$j = 'C:\ntdrive-upgrade\job.json'
'JOB=' + $(if (Test-Path $j) { Get-Content $j -Raw | ConvertFrom-Json | ConvertTo-Json -Compress } else { '' })
$e = 'C:\$WINDOWS.~BT\Sources\Panther\setuperr.log'
'SETUPERR=' + $(if (Test-Path -LiteralPath $e) { (Get-Content -LiteralPath $e -Tail 5) -join ' | ' } else { '' })
$d = (bcdedit.exe /enum '{current}' | Select-String '^debug\s+(\S+)').Matches
'DEBUG=' + $(if ($d) { $d[0].Groups[1].Value } else { '' })
'BOOT=' + (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')
"""

PREPARE_PS = r"""
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName 'ntdrive-upgrade' -Confirm:$false -ErrorAction SilentlyContinue
# The last attempt's work folder and setup's own staging folder, which a cut-off setup leaves
# behind (19 GB live). setup is not running here, the caller checked. robocopy in backup mode
# clears what setup's ACLs keep rd from deleting.
New-Item -ItemType Directory -Force -Path 'C:\nx-empty' | Out-Null
foreach ($path in 'C:\ntdrive-upgrade', 'C:\$WINDOWS.~BT', 'C:\nx') {
  if (Test-Path -LiteralPath $path) {
    $ErrorActionPreference = 'Continue'
    robocopy.exe C:\nx-empty $path /MIR /B /MT:16 /R:0 /W:0 /NFL /NDL /NJH /NJS /NP | Out-Null
    cmd.exe /c "rd /s /q `"$path`"" 2>&1 | Out-Null
    $ErrorActionPreference = 'Stop'
  }
}
cmd.exe /c 'rd /s /q C:\nx-empty' 2>&1 | Out-Null
New-Item -ItemType Directory -Force -Path 'C:\ntdrive-upgrade' | Out-Null
'PREPARED=1'
"""

START_PS = r"""
$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\ntdrive-upgrade\pipeline.ps1'
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 8) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ntdrive-upgrade' -Action $a -Principal $p -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName 'ntdrive-upgrade'
'STARTED=1'
"""

# The pipeline itself. Its task has no trigger (on demand only), so it never runs again by itself.
PIPELINE_PS = r"""# ntdrive vm_upgrade pipeline. Runs as SYSTEM from the on-demand scheduled task
# ntdrive-upgrade. Reads job.json and files.csv beside it, reports in state.json and upgrade.log.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\ntdrive-upgrade'
$job = Get-Content "$root\job.json" -Raw | ConvertFrom-Json
$dl = "$root\files"
$refs = "$root\refs"
$media = "$root\media"
$wimlib = "$root\wimlib\wimlib-imagex.exe"
$log = "$root\upgrade.log"
$extra = @{}
function Log([string]$m) { Add-Content -Path $log -Value ((Get-Date -Format o) + ' ' + $m) }
function State([string]$phase, [string]$detail) {
  $s = [ordered]@{ phase = $phase; detail = $detail; updated = (Get-Date -Format o) }
  foreach ($k in $extra.Keys) { $s[$k] = $extra[$k] }
  ($s | ConvertTo-Json -Compress) | Set-Content -Path "$root\state.json" -Encoding ASCII
  Log "$phase $detail"
}
function Native([string]$exe, [string[]]$a) {
  # Windows PowerShell 5.1 turns a native program's stderr into an error record, which 'Stop'
  # makes fatal (wimlib writes its progress there), so judge the program by its exit code.
  $ErrorActionPreference = 'Continue'
  $out = @(& $exe @a 2>&1 | ForEach-Object { "$_" })
  $code = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  if ($code -ne 0) {
    throw ("{0} exited {1}: {2}" -f [IO.Path]::GetFileName($exe), $code, (($out | Select-Object -Last 3) -join ' '))
  }
  return $out
}
function RemoveTree([string]$path) {
  # rd copes with the long WinSxS-style names inside CABs, PowerShell's cmdlets do not.
  if (Test-Path $path) { cmd.exe /c "rd /s /q $path" | Out-Null }
}
function FreeGb { [math]::Round((Get-PSDrive C).Free / 1GB, 1) }
try {
  $extra['build_started'] = (Get-Date -Format o)
  if ($job.remove_windows_old -and (Test-Path 'C:\Windows.old')) {
    # Its files belong to TrustedInstaller. takeown plus icacls rewrites the ACL of every file
    # and had not finished after 40 minutes on a 21 GB Windows.old (2 vCPUs). robocopy in
    # backup mode (/B) mirrors an empty folder over it, which skips the ACLs and took 60 s.
    State 'windows_old' 'deleting C:\Windows.old, the previous build'
    $t0 = Get-Date
    New-Item -ItemType Directory -Force -Path 'C:\nx-empty' | Out-Null
    $ErrorActionPreference = 'Continue'
    robocopy.exe C:\nx-empty C:\Windows.old /MIR /B /MT:16 /R:0 /W:0 /NFL /NDL /NJH /NJS /NP | Out-Null
    $rc = $LASTEXITCODE
    cmd.exe /c 'rd /s /q C:\Windows.old' 2>&1 | Out-Null
    cmd.exe /c 'rd /s /q C:\nx-empty' 2>&1 | Out-Null
    $ErrorActionPreference = 'Stop'
    if ($rc -ge 8) { throw "robocopy could not clear C:\Windows.old (exit $rc)" }
    $extra['windows_old_s'] = [int]((Get-Date) - $t0).TotalSeconds
    if (Test-Path 'C:\Windows.old') { throw 'C:\Windows.old could not be deleted completely' }
  }
  $free = FreeGb
  if ($free -lt $job.min_free_gb) { throw "only $free GB free on C:, the upgrade needs $($job.min_free_gb)" }
  New-Item -ItemType Directory -Force -Path $dl, $refs | Out-Null
  $rows = @(Import-Csv "$root\files.csv")
  $i = 0
  foreach ($row in $rows) {
    $i++
    if (($i -eq 1) -or ($i % 10 -eq 0)) { State 'download' ("file {0} of {1}" -f $i, $rows.Count) }
    $dest = Join-Path $dl $row.name
    if ((Test-Path $dest) -and ((Get-FileHash $dest -Algorithm SHA1).Hash.ToLower() -eq $row.sha1)) { continue }
    $null = Native 'curl.exe' @('-s', '-f', '-L', '--retry', '3', '--retry-delay', '5', '-o', $dest, $row.url)
    if ((Get-FileHash $dest -Algorithm SHA1).Hash.ToLower() -ne $row.sha1) {
      Remove-Item $dest
      throw "sha1 mismatch on $($row.name)"
    }
  }
  State 'tools' 'unpacking wimlib'
  if (-not (Test-Path $wimlib)) { Expand-Archive "$root\wimlib.zip" -DestinationPath "$root\wimlib" -Force }
  $meta = Join-Path $dl $job.metadata
  State 'media' 'applying the setup media tree with DISM'
  RemoveTree $media
  New-Item -ItemType Directory -Force -Path $media | Out-Null
  $null = Native 'dism.exe' @('/English', '/Apply-Image', "/ImageFile:$meta", '/Index:1', "/ApplyDir:$media")
  $cabs = @(Get-ChildItem "$dl\cabs_*.cab")
  $n = 0
  foreach ($cab in $cabs) {
    $n++
    State 'refs' ("reference image {0} of {1}" -f $n, $cabs.Count)
    $tmp = "C:\nx\$n"
    RemoveTree $tmp
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $null = Native 'expand.exe' @('-f:*', $cab.FullName, $tmp)
    $null = Native $wimlib @('capture', $tmp, "$refs\$n.wim", $cab.BaseName, '--no-acls', '--norpfix')
    RemoveTree $tmp
  }
  RemoveTree 'C:\nx'
  $refArgs = @("--ref=$dl\*.esd")
  if ($cabs.Count -gt 0) { $refArgs += "--ref=$refs\*.wim" }
  $image = "$media\sources\install.wim"
  State 'install_image' 'merging the install image, the slow step'
  # XPRESS, not the default LZX: this image is read once by setup on the same disk, and LZX
  # compression on a 2-vCPU guest made this the longest step of the build (15 minutes).
  $t0 = Get-Date
  $null = Native $wimlib (@('export', $meta, '3', $image, '--compress=XPRESS') + $refArgs)
  $extra['install_image_s'] = [int]((Get-Date) - $t0).TotalSeconds
  State 'winre' 'adding WinRE to the install image'
  $null = Native $wimlib (@('export', $meta, '2', "$root\winre.wim") + $refArgs)
  $null = Native $wimlib @('update', $image, '1', "--command=add $root\winre.wim \Windows\System32\Recovery\Winre.wim")
  $info = Native $wimlib @('info', $image, '1')
  $built = ($info | Select-String '^Build:\s+(\d+)' | Select-Object -First 1).Matches[0].Groups[1].Value
  $edition = ($info | Select-String '^Edition ID:\s+(\S+)' | Select-Object -First 1).Matches[0].Groups[1].Value
  if ($built -ne [string]$job.target_build) { throw "the built image is build $built, expected $($job.target_build)" }
  if ($edition -ne $job.edition_id) { throw "the built image is edition $edition, the guest runs $($job.edition_id)" }
  State 'cleanup' 'removing the downloads and reference images'
  RemoveTree $dl
  RemoveTree $refs
  Remove-Item "$root\winre.wim" -ErrorAction SilentlyContinue
  $free = FreeGb
  if ($free -lt $job.min_free_setup_gb) { throw "only $free GB free on C: for setup, it needs $($job.min_free_setup_gb)" }
  $d = (bcdedit.exe /enum '{current}' | Select-String '^debug\s+(\S+)').Matches
  $extra['debug_before'] = $(if ($d) { $d[0].Groups[1].Value } else { '' })
  if ($extra['debug_before'] -eq 'Yes') { $null = Native 'bcdedit.exe' @('/debug', 'off') }
  State 'setup' 'setup.exe is upgrading, the guest reboots several times'
  $setupArgs = '/auto upgrade /quiet /eula accept /dynamicupdate disable /compat ignorewarning /showoobe none /telemetry disable /copylogs C:\ntdrive-upgrade\setup-logs'
  $proc = Start-Process -FilePath "$media\setup.exe" -ArgumentList $setupArgs -Wait -PassThru
  $extra['setup_exit'] = ('0x{0:X8}' -f $proc.ExitCode)
  State 'setup_exited' ("setup.exe exited {0}" -f $extra['setup_exit'])
} catch {
  State 'failed' $_.Exception.Message
}
"""


def _parse_probe(out: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isupper() and key.replace("_", "").isalpha():
            values[key] = value.strip()
    return values


def _json_or_empty(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {"unreadable": text[:200]}
    return value if isinstance(value, dict) else {}


async def _probe(transport: Any) -> dict[str, str]:
    code, out = await transport.exec_once(_encoded(PROBE_PS), timeout=90)
    values = _parse_probe(out)
    if "BUILD" not in values:
        raise NtDriveError(
            BACKEND_ERROR,
            f"could not read the guest's build (exit {code}): {out.strip()[:200]}",
            "the SSH account must be an administrator, check with term_exec",
        )
    return values


def _select_files(
    files: dict[str, Any], edition_id: str, lang: str
) -> tuple[list[dict[str, str]], str, int]:
    """The files the pipeline needs, each checked: Microsoft host, safe name, SHA-1 present."""
    metadata_name = f"metadataesd_{edition_id.lower()}_{lang}.esd"
    rows: list[dict[str, str]] = []
    metadata = ""
    total = 0
    for name, meta in sorted(files.items()):
        lower = name.lower()
        is_metadata = lower.startswith("metadataesd_")
        if is_metadata and lower != metadata_name:
            continue
        if not (lower.endswith(".esd") or (lower.startswith("cabs_") and lower.endswith(".cab"))):
            continue
        url = str(meta.get("url", ""))
        sha1 = str(meta.get("sha1", "")).lower()
        host = urlparse(url).hostname or ""
        if not _SAFE_NAME.match(name) or not _SHA1.match(sha1):
            raise NtDriveError(
                BACKEND_ERROR, f"the UUP dump file list has a malformed entry: {name}"
            )
        if not (host == "microsoft.com" or host.endswith(".microsoft.com")):
            raise NtDriveError(
                BACKEND_ERROR,
                f"{name} would come from {host}, not a Microsoft host, so nothing was started",
            )
        if is_metadata:
            metadata = name
        rows.append({"name": name, "url": url, "sha1": sha1, "size": str(meta.get("size", ""))})
        total += int(meta.get("size") or 0)
    if not metadata:
        raise NtDriveError(
            BACKEND_ERROR,
            f"the build's file list has no {metadata_name}",
            "the edition or language is missing from this build on UUP dump",
        )
    return rows, metadata, total


def _csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["name", "url", "sha1", "size"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


# -- parameters ------------------------------------------------------------------------------


class BuildsParams(BaseModel):
    """vm_upgrade_builds."""

    search: str = Field(
        default=_INSIDER_TITLE,
        max_length=80,
        description="Text the build title must contain, for example a build number like 29698",
    )
    limit: int = Field(default=10, ge=1, le=50, description="How many builds to return")


class UpgradeParams(VmParams, ConfirmMixin):
    """vm_upgrade."""

    build: str = Field(
        default="latest",
        description=(
            "Target Insider build: latest, or a build number such as 29698 (or 29698.1000). "
            "It must be newer than the guest, setup cannot downgrade"
        ),
    )
    remove_windows_old: bool = Field(
        default=False,
        description=(
            "Delete C:\\Windows.old first, the previous build an earlier upgrade left behind "
            "(about 20 GB, so a second upgrade in a row is short of space without this). "
            "Settings' go-back uses it, so only for a guest whose way back is a snapshot"
        ),
    )


class UpgradeStatusParams(VmParams):
    """vm_upgrade_status."""


# -- tools -----------------------------------------------------------------------------------


@tool(
    "vm_upgrade_builds",
    "List Windows 11 Insider Preview builds (amd64) that vm_upgrade can install, newest first, "
    "from the UUP dump API.",
    BuildsParams,
    positional=(),
    effect="read",
    idempotent=True,
)
async def vm_upgrade_builds(service: NtDriveService, p: BuildsParams) -> dict[str, Any]:
    """Builds known to UUP dump."""
    builds = await _insider_builds(p.search)
    return {"builds": builds[: p.limit], "source": UUP_API}


@tool(
    "vm_upgrade",
    "Upgrade a running guest in place to a Windows Insider build. The guest downloads the build "
    "from Microsoft, builds setup media and runs setup.exe as SYSTEM, which reboots it several "
    "times over 30 to 90 minutes. Returns at once: poll vm_upgrade_status. Edition and language "
    "follow the guest. Take a snapshot first, and kd_detach (refused while kd is attached). "
    f"Needs {MIN_FREE_GB:.0f} GB free on C:. confirm=true.",
    UpgradeParams,
    positional=("vm",),
    touches_guest=True,
    effect="destructive",
    destructive=True,
)
async def vm_upgrade(service: NtDriveService, p: UpgradeParams) -> dict[str, Any]:
    """Resolve the build, stage the pipeline in the guest and start it."""
    if not _BUILD_ARG.match(p.build):
        raise NtDriveError(INVALID_ARGS, f"build must be latest or a build number, not {p.build!r}")
    cfg = service.vm_cfg(p.vm)
    if service.runtime(p.vm).kd_state != KdState.DETACHED:
        raise NtDriveError(
            KD_ALREADY_ATTACHED,
            f"a debugger is attached to {p.vm}",
            "kd_detach first: setup reboots into its own environment several times, and KDNET "
            "across those boots can NMI the guest. kd_setup_guest and kd_attach after the upgrade",
        )
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    transport = await service.transport(cfg)
    if not hasattr(transport, "exec_once"):
        raise NtDriveError(BACKEND_ERROR, "the transport cannot run commands")
    guest = await _probe(transport)
    if guest.get("TASK") == "Running" or guest.get("SETUP_RUNNING") == "True":
        raise NtDriveError(
            INVALID_ARGS,
            f"an upgrade is already running in {p.vm}",
            "follow it with vm_upgrade_status",
        )
    current = guest["BUILD"]
    edition_id = guest.get("EDITION", "")
    lang = guest.get("LANG", "")
    if not _LANG.match(lang) or not _SAFE_NAME.match(edition_id):
        raise NtDriveError(
            BACKEND_ERROR,
            f"could not read the guest's edition and language ({edition_id!r}, {lang!r})",
        )
    # Deleting Windows.old is the pipeline's first step, not part of this call (it can take
    # minutes), and the pipeline checks the free space again afterwards.
    remove_old = p.remove_windows_old and guest.get("WINDOWS_OLD") == "True"

    search = _INSIDER_TITLE if p.build == "latest" else p.build.split(".")[0]
    builds = await _insider_builds(search)
    if p.build != "latest":
        builds = [b for b in builds if p.build in (b["build"], b["build"].split(".")[0])]
    if not builds:
        raise NtDriveError(
            INVALID_ARGS,
            f"UUP dump lists no amd64 Insider build {p.build}",
            "vm_upgrade_builds lists what is available",
        )
    target = builds[0]
    target_build = target["build"].split(".")[0]
    if int(target_build) <= int(current):
        raise NtDriveError(
            INVALID_ARGS,
            f"{p.vm} already runs build {current}, the target is {target['build']}",
            "setup cannot downgrade or reinstall the same build in place",
        )

    editions = await _uup("listeditions.php", id=target["uuid"], lang=lang)
    available = [str(e).upper() for e in editions.get("editionList") or []]
    if edition_id.upper() not in available:
        raise NtDriveError(
            INVALID_ARGS,
            f"build {target['build']} has no {edition_id} edition in {lang} (it has "
            f"{', '.join(available) or 'none'})",
            "an in-place upgrade keeps the guest's edition and language, so they must match",
        )
    listing = await _uup("get.php", id=target["uuid"], lang=lang, edition=edition_id.lower())
    rows, metadata, total = _select_files(listing.get("files") or {}, edition_id, lang)

    wimlib = await _wimlib_zip()
    job = {
        "target_build": target_build,
        "target_full": target["build"],
        "uuid": target["uuid"],
        "edition_id": edition_id,
        "lang": lang,
        "metadata": metadata,
        "from_build": f"{current}.{guest.get('UBR', '')}",
        "min_free_gb": MIN_FREE_GB,
        "min_free_setup_gb": MIN_FREE_SETUP_GB,
        "remove_windows_old": remove_old,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Everything that can be refused without touching the guest has been checked. Clearing the
    # last attempt's leftovers comes before the space check: live, a setup cut off mid-copy left
    # 19.2 GB in C:\$WINDOWS.~BT and 4.7 GB of media, and the retry was refused at 27.9 GB free.
    _, out = await transport.exec_once(_encoded(PREPARE_PS), timeout=900)
    if "PREPARED=1" not in out:
        raise NtDriveError(
            BACKEND_ERROR,
            f"could not clear the previous attempt in {p.vm}: {out.strip()[-300:]}",
            "the SSH account must be an administrator",
        )
    guest = await _probe(transport)
    free = float(guest.get("FREE_GB") or 0)
    if free < MIN_FREE_GB and not remove_old:
        hint = "free space in the guest or grow the virtual disk"
        if guest.get("WINDOWS_OLD") == "True":
            hint = (
                "C:\\Windows.old from an earlier upgrade holds about 20 GB: retry with "
                "remove_windows_old=true (snapshot first, it is what Settings' go-back uses)"
            )
        raise NtDriveError(
            INVALID_ARGS,
            f"{p.vm} has {free:.1f} GB free on C:, the upgrade needs {MIN_FREE_GB:.0f}",
            hint,
        )
    with tempfile.TemporaryDirectory(prefix="ntdrive-upgrade-") as tmp:
        staged = {
            "job.json": json.dumps(job, indent=1),
            "files.csv": _csv(rows),
            "pipeline.ps1": PIPELINE_PS,
        }
        for name, text in staged.items():
            local = os.path.join(tmp, name)
            with open(local, "w", encoding="ascii", newline="\r\n") as handle:
                handle.write(text)
            await transport.put_file(local, f"{GUEST_ROOT}\\{name}")
        await transport.put_file(wimlib, f"{GUEST_ROOT}\\wimlib.zip")
    _, out = await transport.exec_once(_encoded(START_PS), timeout=120)
    if "STARTED=1" not in out:
        raise NtDriveError(
            BACKEND_ERROR,
            f"could not start the upgrade task in {p.vm}: {out.strip()[:300]}",
            "the SSH account needs administrator rights to register a SYSTEM task",
        )
    service.state.record_event(p.vm, "vm_upgrade", build=target["build"], from_build=current)
    return {
        "vm": p.vm,
        "from_build": job["from_build"],
        "target_build": target["build"],
        "title": target["title"],
        "edition": edition_id,
        "lang": lang,
        "files": len(rows),
        "download_bytes": total,
        "cpus": guest.get("CPUS"),
        "free_gb": free,
        "removes_windows_old": remove_old,
        "note": (
            "started in the guest. Poll vm_upgrade_status every few minutes: building takes 20 "
            "to 40 minutes, then setup reboots the guest several times. Terminal sessions drop "
            "at the first reboot"
        ),
    }


@tool(
    "vm_upgrade_status",
    "Where a vm_upgrade stands: building (with the step), upgrading, offline (setup is between "
    "reboots), done, failed (with setup's own error lines), interrupted or none. When done it "
    "names the steps that finish the job: kd_setup_guest, vm_reboot, kd_attach, a snapshot.",
    UpgradeStatusParams,
    effect="read",
    idempotent=True,
)
async def vm_upgrade_status(service: NtDriveService, p: UpgradeStatusParams) -> dict[str, Any]:
    """Read the pipeline's state file and the guest's build."""
    cfg = service.vm_cfg(p.vm)
    service.ensure_not_frozen(p.vm)
    await service.ensure_running(cfg)
    offline = {
        "vm": p.vm,
        "status": "offline",
        "note": (
            "SSH does not answer, which is normal while setup applies the build between "
            "reboots. Poll again in a few minutes. vm_wait_ready blocks until SSH is back"
        ),
    }
    try:
        reachable, _ip = await service.ssh_reachable(cfg, timeout=15)
    except NtDriveError:
        # In setup's own boot environment VMware Tools report no address at all.
        reachable = False
    if not reachable:
        return offline
    try:
        transport = await service.transport(cfg)
        guest = await _probe(transport)
    except NtDriveError as exc:
        # Seen live: the port answered, then the probe failed as setup took the guest down for
        # its first reboot. That is the same state as no answer at all, not an error.
        return {**offline, "probe_error": exc.message}
    job = _json_or_empty(guest.get("JOB", ""))
    state = _json_or_empty(guest.get("STATE", ""))
    return _status(p.vm, guest, job, state)


def _status(
    vm: str, guest: dict[str, str], job: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "vm": vm,
        "build": f"{guest['BUILD']}.{guest.get('UBR', '')}",
        "target_build": job.get("target_full") or job.get("target_build"),
        "phase": state.get("phase"),
        "detail": state.get("detail"),
        "updated": state.get("updated"),
        "debug": guest.get("DEBUG"),
    }
    for key in ("build_started", "windows_old_s", "install_image_s", "debug_before"):
        if key in state:
            result[key] = state[key]
    phase = state.get("phase")
    if not job:
        result["status"] = "none"
        return result
    if guest["BUILD"] == str(job.get("target_build")):
        result["status"] = "done"
        result["next"] = [
            "kd_setup_guest (setup rewrote the boot entry and debugging was switched off)",
            "vm_reboot mode=soft",
            "kd_attach, then kd_symcheck for the new kernel",
            "snap_take a base snapshot for the new build",
            "file_delete C:\\ntdrive-upgrade\\media recurse=true frees about 5 GB",
        ]
        return result
    if phase in _BUILDING:
        running = guest.get("TASK") == "Running"
        result["status"] = "building" if running else "interrupted"
        if not running:
            result["note"] = "the pipeline stopped mid-step (a reboot?), call vm_upgrade again"
        return result
    if phase == "failed":
        result["status"] = "failed"
        if "curl.exe exited" in str(state.get("detail")):
            result["note"] = (
                "a download failed. The CDN links in the file list expire after a while, so "
                "call vm_upgrade again for fresh ones"
            )
        return result
    setup_exit = str(state.get("setup_exit", ""))
    if guest.get("SETUP_RUNNING") == "True":
        result["status"] = "upgrading"
        return result
    # setuperr.log is not a verdict: a healthy setup also logs errors there (seen live: a MIG
    # CSIAgent "Invalid xml format" line mid-upgrade). Only setup's own exit code is.
    errors = guest.get("SETUPERR", "")
    if _setup_failed(setup_exit):
        result["status"] = "failed"
        result["setup_exit"] = setup_exit
        result["setup_errors"] = errors
        result["note"] = (
            "setup.exe failed and the guest still runs the old build. setup_errors holds the last "
            "lines of setuperr.log, and C:\\ntdrive-upgrade\\setup-logs the full logs"
        )
        return result
    result["setup_errors"] = errors
    booted, updated = _when(guest.get("BOOT", "")), _when(str(state.get("updated", "")))
    if booted is not None and updated is not None and booted > updated:
        # The guest booted after the pipeline's last word and still runs the old build, with
        # setup gone: setup was cut off (seen live: the VM was powered off mid-copy) or rolled
        # back. Either way the next step is the same.
        result["status"] = "interrupted"
        result["note"] = (
            "the guest rebooted before setup finished and still runs the old build (a power cut, "
            "or setup rolled back). Call vm_upgrade again. If it ends here twice, setup_errors "
            "and C:\\$WINDOWS.~BT\\Sources\\Panther\\setupact.log say why"
        )
        return result
    result["status"] = "stalled"
    result["note"] = (
        "setup is not running and the guest is still on the old build. That is normal for a "
        "moment between setup's own steps, so poll again in a few minutes. If it stays, setup "
        "rolled back: C:\\$WINDOWS.~BT\\Sources\\Panther\\setupact.log says where it stopped"
    )
    return result


def _when(text: str) -> datetime | None:
    """A PowerShell round-trip time ('o' format, 7 fraction digits) as an aware datetime."""
    match = re.match(
        r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$", text.strip()
    )
    if match is None:
        return None
    stamp = datetime.fromisoformat(match.group(1) + (match.group(2) or "").replace("Z", "+00:00"))
    return stamp if stamp.tzinfo is not None else None


def _setup_failed(code: str) -> bool:
    """True for a setup.exe exit code that reports failure (0xC19..., 0x8...), not for success."""
    try:
        value = int(code, 16)
    except ValueError:
        return False
    return value & 0x80000000 != 0

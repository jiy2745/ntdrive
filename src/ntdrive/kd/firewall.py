"""Host firewall for KDNET: can kd.exe receive UDP, and one UAC prompt to make it so.

kd.exe listens on a UDP port and the guest sends to it, so the host firewall needs an inbound
Allow rule for kd.exe. Windows adds inbound Block rules for a program when its first listen
raises the firewall prompt and nobody clicks Allow, and a Block rule wins over any Allow rule.
Reading the rules needs no privilege. Changing them needs an administrator, so the repair runs
a short script through Start-Process -Verb RunAs, which shows exactly one UAC prompt.
scripts/setup-host.ps1 -FirewallOnly runs the same script from an elevated shell.
"""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ntdrive.errors import BACKEND_ERROR, TIMEOUT, NtDriveError
from ntdrive.hostproc import run_hidden

ALLOW_RULE = "ntdrive kd.exe KDNET"
POWERSHELL = "powershell.exe"
CHECK_TIMEOUT = 30.0
MANUAL_FIREWALL_HINT = "run scripts/setup-host.ps1 -FirewallOnly as Administrator once"
TOOL_FIREWALL_HINT = "run kd_setup_host, one UAC prompt"


@dataclass
class FirewallStatus:
    """What the host firewall says about inbound traffic to kd.exe."""

    allow: bool = False
    block_rules: list[str] = field(default_factory=list)
    checked: bool = True
    error: str = ""

    @property
    def ok(self) -> bool:
        """An enabled Allow rule for UDP and no enabled Block rule."""
        return self.checked and self.allow and not self.block_rules

    @property
    def hint(self) -> str:
        """What to do: the tool when the rules could be read, the manual route otherwise."""
        return TOOL_FIREWALL_HINT if self.checked else MANUAL_FIREWALL_HINT

    def as_dict(self) -> dict[str, Any]:
        """The wire form used by kd_setup_host and sys_health."""
        return {
            "ok": self.ok,
            "allow_rule": self.allow,
            "block_rules": list(self.block_rules),
            "checked": self.checked,
            "error": self.error or None,
        }

    def problem(self) -> str:
        """One line that says what is wrong."""
        if not self.checked:
            return f"host firewall rules could not be read: {self.error}"
        parts: list[str] = []
        if self.block_rules:
            parts.append(f"inbound Block rules for kd.exe: {', '.join(self.block_rules)}")
        if not self.allow:
            parts.append("no inbound Allow rule for kd.exe UDP")
        return "host firewall blocks KDNET (" + ", ".join(parts) + ")"

    def issue(self) -> str:
        """The problem and its fix, the form sys_health puts in a VM's issues."""
        return f"{self.problem()} ({self.hint})"


FirewallCheck = Callable[[str], Awaitable[FirewallStatus]]
FirewallFix = Callable[[str, float], Awaitable[FirewallStatus]]

# Shared by the check and the fix: the inbound rules whose program is the configured kd.exe.
# Query User rules store the path lower case and other rules may use environment variables, so
# both sides are expanded, backslashed and lower cased before the comparison.
_PRELUDE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$kd = @KD@
$kdl = $kd.ToLowerInvariant()
function Get-KdRules {
  foreach ($f in (Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue)) {
    $prog = [Environment]::ExpandEnvironmentVariables([string]$f.Program)
    if ($prog.Replace('/', '\').ToLowerInvariant() -ne $kdl) { continue }
    $rule = $f | Get-NetFirewallRule -ErrorAction SilentlyContinue
    if ($rule -and "$($rule.Direction)" -eq 'Inbound') { $rule }
  }
}
"""

_CHECK_BODY = r"""
$allow = $false
$block = @()
foreach ($rule in @(Get-KdRules)) {
  if ("$($rule.Enabled)" -ne 'True') { continue }
  if ("$($rule.Action)" -eq 'Block') { $block += [string]$rule.DisplayName; continue }
  $proto = "$(($rule | Get-NetFirewallPortFilter).Protocol)"
  if ("$($rule.Action)" -eq 'Allow' -and ($proto -eq 'UDP' -or $proto -eq 'Any')) { $allow = $true }
}
@{ allow = $allow; block = @($block) } | ConvertTo-Json -Compress
"""

# Recreate the Allow rule rather than enable one by name: a rule with this name left by an older
# setup may point at another kd.exe, and the check only credits the configured path.
_FIX_BODY = r"""
foreach ($rule in @(Get-KdRules)) {
  if ("$($rule.Action)" -eq 'Block') { Remove-NetFirewallRule -Name $rule.Name }
}
Remove-NetFirewallRule -DisplayName @ALLOW@ -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName @ALLOW@ -Direction Inbound -Program $kd -Protocol UDP `
  -Action Allow -Profile Any | Out-Null
"""

# Runs the fix script elevated, handed over as -EncodedCommand so no file is involved and the
# path survives any code page. Start-Process throws when the UAC prompt is refused, which ends
# this launcher with a non-zero exit code and the message on its output.
_LAUNCHER = r"""
$ErrorActionPreference = 'Stop'
$list = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden'
$list = $list + ' -EncodedCommand @ENCODED@'
$p = Start-Process -FilePath 'powershell.exe' -ArgumentList $list -Verb RunAs `
  -WindowStyle Hidden -PassThru -Wait
exit $p.ExitCode
"""


def _literal(value: str) -> str:
    """A single-quoted PowerShell string literal."""
    return "'" + value.replace("'", "''") + "'"


def _kd_path(kd: str) -> str:
    return str(Path(kd).resolve())


def check_script(kd: str) -> str:
    """PowerShell that prints {allow, block} as JSON for the rules that name kd.exe."""
    return _PRELUDE.replace("@KD@", _literal(_kd_path(kd))) + _CHECK_BODY


def fix_script(kd: str) -> str:
    """PowerShell (needs elevation): drop Block rules for kd.exe, recreate the Allow rule."""
    prelude = _PRELUDE.replace("@KD@", _literal(_kd_path(kd)))
    return prelude + _FIX_BODY.replace("@ALLOW@", _literal(ALLOW_RULE))


def _encoded(script: str) -> str:
    """-EncodedCommand form, which sidesteps every quoting and code page rule."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


async def _run_powershell(script: str, timeout: float) -> tuple[int, str]:
    """Run a script unelevated and without a console window. Returns (exit code, output)."""
    argv = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    return await run_hidden([*argv, "-EncodedCommand", _encoded(script)], timeout)


def parse_check_output(code: int, out: str) -> FirewallStatus:
    """Turn the check script's exit code and output into a status. Never raises."""
    line = next((ln for ln in reversed(out.splitlines()) if ln.strip().startswith("{")), "")
    if code != 0 or not line:
        detail = out.strip()[-300:] or f"powershell exit {code}"
        return FirewallStatus(checked=False, error=detail)
    try:
        data = json.loads(line)
    except ValueError:
        return FirewallStatus(checked=False, error=f"unexpected output: {line[:200]}")
    block = data.get("block") or []
    if isinstance(block, str):
        block = [block]
    return FirewallStatus(allow=bool(data.get("allow")), block_rules=[str(b) for b in block])


async def firewall_status(kd: str) -> FirewallStatus:
    """Read the rules for kd.exe. Never raises: an unreadable firewall is reported as such."""
    if sys.platform != "win32":
        return FirewallStatus(checked=False, error="not a Windows host")
    try:
        code, out = await _run_powershell(check_script(kd), CHECK_TIMEOUT)
    except NtDriveError as exc:
        return FirewallStatus(checked=False, error=exc.message)
    except OSError as exc:
        return FirewallStatus(checked=False, error=str(exc))
    return parse_check_output(code, out)


async def firewall_fix(kd: str, timeout: float) -> FirewallStatus:
    """Repair the rules through one UAC prompt, then read them again."""
    if sys.platform != "win32":
        raise NtDriveError(
            BACKEND_ERROR, "the host firewall can only be changed on Windows", MANUAL_FIREWALL_HINT
        )
    launcher = _LAUNCHER.replace("@ENCODED@", _encoded(fix_script(kd)))
    try:
        code, out = await _run_powershell(launcher, timeout)
    except NtDriveError as exc:
        if exc.code == TIMEOUT:
            # The consent dialog outlives the killed launcher. Approving it later still runs
            # the repair, because the script travels inside the command line, not in a file.
            raise NtDriveError(
                TIMEOUT,
                f"no answer to the UAC prompt within {timeout:.0f}s",
                "approve the prompt (the repair then runs on its own) and call kd_setup_host "
                f"again to confirm, or {MANUAL_FIREWALL_HINT}",
            ) from None
        raise
    if code != 0:
        text = out.strip()
        if "cancel" in text.lower():
            message = "the UAC prompt was refused"
        else:
            message = f"the firewall repair failed: {text[-300:] or f'exit code {code}'}"
        raise NtDriveError(
            BACKEND_ERROR,
            message,
            f"approve the UAC prompt when kd_setup_host asks, or {MANUAL_FIREWALL_HINT}",
        )
    return await firewall_status(kd)

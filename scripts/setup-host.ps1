<#
.SYNOPSIS
  Prepare a Windows 11 host for ntdrive.

.DESCRIPTION
  Checks vmrun.exe, kd.exe and kdnet.exe, installs the Python environment with uv, installs the
  pre-commit hooks, creates vms.yaml from the example when missing, prints the VMnet8 host IP to
  use as kdnet_hostip, and adds a Windows Firewall rule so kd.exe can receive KDNET packets.
  Run from an elevated PowerShell for the firewall step; everything else works unelevated.
#>

[CmdletBinding()]
param(
  [string]$Vmrun = "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
  [string]$Kd = "C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\kd.exe",
  [switch]$SkipFirewall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "== binaries"
foreach ($p in @($Vmrun, $Kd, (Join-Path (Split-Path $Kd) "kdnet.exe"))) {
  if (Test-Path $p) { Write-Host "ok      $p" } else { Write-Warning "missing $p" }
}

Write-Host "== python environment"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv is not installed. See https://docs.astral.sh/uv/"
}
Push-Location $root
try {
  uv sync
  uv run pre-commit install
} finally {
  Pop-Location
}

Write-Host "== config"
$cfg = Join-Path $root "vms.yaml"
if (-not (Test-Path $cfg)) {
  Copy-Item (Join-Path $root "vms.example.yaml") $cfg
  Write-Host "created $cfg from the example; edit it before use"
}

Write-Host "== VMnet8 host address (use as kdnet_hostip)"
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.InterfaceAlias -like "*VMnet8*" } |
  ForEach-Object { Write-Host "  $($_.InterfaceAlias): $($_.IPAddress)" }

if (-not $SkipFirewall) {
  Write-Host "== firewall for kd.exe (KDNET UDP inbound)"
  $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($isAdmin) {
    # A Block rule wins over an Allow rule, so any pre-existing inbound Block rule that points at
    # kd.exe must go first. Windows sometimes auto-creates one the first time kd.exe binds a port.
    $blocked = Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
      Where-Object { $_.Program -like "*\kd.exe" } |
      ForEach-Object { $_ | Get-NetFirewallRule } |
      Where-Object { $_.Direction -eq "Inbound" -and $_.Action -eq "Block" }
    foreach ($r in $blocked) {
      Remove-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue
      Write-Host "  removed blocking rule: $($r.DisplayName)"
    }
    if (-not (Get-NetFirewallRule -DisplayName "ntdrive kd.exe KDNET" -ErrorAction SilentlyContinue)) {
      New-NetFirewallRule -DisplayName "ntdrive kd.exe KDNET" -Direction Inbound -Program $Kd `
        -Protocol UDP -Action Allow -Profile Any | Out-Null
      Write-Host "  allow rule created"
    } else {
      Write-Host "  allow rule already present"
    }
  } else {
    Write-Warning "not elevated. KDNET needs this: rerun this script as Administrator."
    Write-Warning "It removes any inbound Block rule for kd.exe and adds an Allow rule."
  }
}

Write-Host "done. Next: uv run ntdrive sys health"

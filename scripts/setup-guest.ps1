<#
.SYNOPSIS
  Prepare a Windows 10/11 guest for ntdrive. Run it inside the guest from any PowerShell: it asks
  for administrator rights itself, one UAC click.

.DESCRIPTION
  Installs and starts OpenSSH Server with PowerShell as the default shell, and optionally enables
  kernel debugging with bcdedit: -HostIp for KDNET (the ntdrive default) or -Serial for the serial
  named-pipe transport. Without either, kd_setup_guest does that part over SSH later. Reboot the
  guest afterwards when debugging was configured.

  OpenSSH comes from the Windows capability (Feature on Demand) when Windows can install it. When
  that fails, which is normal on Insider builds because Windows Update publishes no capability
  package for them, the script falls back to the Win32-OpenSSH zip: it downloads OpenSSH-Win64.zip
  from GitHub (or takes -OpenSshZip, a local path or URL), expands it to C:\Program Files\OpenSSH
  and runs its install-sshd.ps1. A guest that already has an sshd service keeps it, so the script
  is safe to run again.

  The kd_setup_guest tool performs the same bcdedit steps over SSH. This script exists for the very
  first setup, when SSH is not available yet. ntdrive unloads PSReadLine per terminal session
  itself, so no profile change is needed here.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup-guest.ps1
  The usual first step: OpenSSH only. kd_setup_guest configures the debugger over SSH afterwards.
  Right click, "Run with PowerShell" does the same. Both end in one UAC prompt.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup-guest.ps1 -Serial
  One command for a fresh guest on the serial transport. Reboot when it says so.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup-guest.ps1 -Serial -OpenSshZip D:\OpenSSH-Win64.zip
  Same, for a guest without internet access. Copy the zip in first.
#>

[CmdletBinding()]
param(
  [switch]$Serial,
  [string]$HostIp,
  [int]$Port = 50000,
  [string]$Key,
  [string]$OpenSshZip = "https://github.com/PowerShell/Win32-OpenSSH/releases/latest/download/OpenSSH-Win64.zip"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  # Everything below needs administrator rights (a service, HKLM, a firewall rule, bcdedit).
  # Relaunch elevated with the same arguments: one UAC click instead of opening an admin shell.
  $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", "`"$PSCommandPath`"")
  foreach ($bound in $PSBoundParameters.GetEnumerator()) {
    if ($bound.Value -is [switch]) {
      if ($bound.Value.IsPresent) { $relaunch += "-$($bound.Key)" }
    } else {
      $relaunch += @("-$($bound.Key)", "`"$($bound.Value)`"")
    }
  }
  Write-Host "administrator rights are needed: approve the UAC prompt, the script continues in the new window"
  try {
    Start-Process -FilePath "powershell.exe" -ArgumentList $relaunch -Verb RunAs | Out-Null
  } catch {
    throw "the UAC prompt was refused. Approve it, or open an Administrator PowerShell and run the script there."
  }
  exit 0
}

function Assert-SecureBootOff {
  # bcdedit /debug on is refused while Secure Boot is on. BIOS firmware has no Secure Boot and
  # Confirm-SecureBootUEFI throws there, which means there is nothing to check.
  try { $on = Confirm-SecureBootUEFI } catch { return }
  if ($on) {
    throw "Secure Boot is on, so bcdedit /debug on would be refused. Power off the VM, turn Secure Boot off (VM settings > Options > Advanced), then run this script again."
  }
}

function Install-OpenSshCapability {
  # True when the Windows capability is installed (already, or by this call). False when Windows
  # cannot provide it: no OpenSSH.Server capability listed, or Add-WindowsCapability failed.
  try {
    $cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
  } catch {
    Write-Host "  Get-WindowsCapability failed: $($_.Exception.Message)"
    return $false
  }
  if (-not $cap) {
    Write-Host "  this build lists no OpenSSH.Server capability"
    return $false
  }
  if ($cap.State -eq "Installed") {
    Write-Host "  capability $($cap.Name) already installed"
    return $true
  }
  try {
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    Write-Host "  installed capability $($cap.Name)"
    return $true
  } catch {
    Write-Host "  Add-WindowsCapability failed: $($_.Exception.Message)"
    return $false
  }
}

function Install-OpenSshZip([string]$Source) {
  # Win32-OpenSSH from its GitHub zip. Build independent, so it works where the capability does not.
  $dest = Join-Path $env:ProgramFiles "OpenSSH"
  $installer = Join-Path $dest "install-sshd.ps1"
  if (Test-Path $installer) {
    Write-Host "  reusing the files already in $dest"
  } else {
    $work = Join-Path $env:TEMP "ntdrive-openssh"
    if (Test-Path $work) { Remove-Item $work -Recurse -Force }
    New-Item -ItemType Directory -Path $work | Out-Null
    $zip = Join-Path $work "OpenSSH-Win64.zip"
    if ($Source -match "^https?://") {
      Write-Host "  downloading $Source"
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
      Invoke-WebRequest -Uri $Source -OutFile $zip -UseBasicParsing
    } else {
      Write-Host "  using $Source"
      Copy-Item $Source $zip
    }
    Unblock-File $zip
    Expand-Archive -Path $zip -DestinationPath $work -Force
    $unpacked = Join-Path $work "OpenSSH-Win64"
    if (-not (Test-Path (Join-Path $unpacked "install-sshd.ps1"))) {
      throw "the zip does not contain OpenSSH-Win64\install-sshd.ps1. Is it OpenSSH-Win64.zip from the Win32-OpenSSH releases?"
    }
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $unpacked $dest
    Get-ChildItem $dest -Recurse | Unblock-File
    Remove-Item $work -Recurse -Force
    Write-Host "  expanded to $dest"
  }
  # install-sshd.ps1 registers the sshd and ssh-agent services and fixes permissions. -Confirm:$false
  # keeps its ShouldProcess helpers from prompting.
  & $installer -Confirm:$false
}

Write-Host "== OpenSSH Server"
if (Get-Service sshd -ErrorAction SilentlyContinue) {
  Write-Host "  sshd service already present, keeping it"
} elseif (-not (Install-OpenSshCapability)) {
  Write-Host "  falling back to the Win32-OpenSSH zip"
  Install-OpenSshZip $OpenSshZip
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
$regPath = "HKLM:\SOFTWARE\OpenSSH"
if (-not (Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
New-ItemProperty -Path $regPath -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
    -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
$sshdPath = (Get-CimInstance Win32_Service -Filter "Name='sshd'").PathName
Write-Host "  sshd running ($sshdPath), default shell = powershell"

if ($Serial) {
  Write-Host "== kernel debugging over the serial pipe (COM1)"
  Assert-SecureBootOff
  bcdedit /debug on | Out-Null
  bcdedit /dbgsettings serial debugport:1 baudrate:115200
  Write-Host "  reboot this guest, then kd_attach from the host"
} elseif ($HostIp) {
  Write-Host "== KDNET"
  Assert-SecureBootOff
  bcdedit /debug on | Out-Null
  if ($Key) {
    bcdedit /dbgsettings net hostip:$HostIp port:$Port key:$Key
  } else {
    bcdedit /dbgsettings net hostip:$HostIp port:$Port
  }
  Write-Host "  copy the key above into vms.yaml (kdnet.key) and reboot this guest"
} else {
  Write-Host "== kernel debugging skipped (no -Serial or -HostIp). kd_setup_guest can do it over SSH later."
}

Write-Host "== for vms.yaml on the host"
$user = $identity.Name.Split('\')[-1]
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
  Select-Object -ExpandProperty IPAddress
$listening = [bool](Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue)
Write-Host "  guest.user: $user  (this account needs a password, SSH refuses empty ones)"
Write-Host "  guest IPv4: $($ips -join ', ')  (ntdrive finds it through VMware Tools, this is for a manual ssh test)"
Write-Host "  sshd listening on 22: $listening"
Write-Host "done"

<#
.SYNOPSIS
  Prepare a Windows 10/11 guest for ntdrive. Run inside the guest as Administrator.

.DESCRIPTION
  Installs and starts OpenSSH Server with PowerShell as the default shell, and optionally enables
  kernel debugging with bcdedit: -Serial for the serial named-pipe transport (the ntdrive default)
  or -HostIp for KDNET. Reboot the guest afterwards when debugging was configured. The
  kd_setup_guest tool performs the same bcdedit steps over SSH; this script exists for the very
  first setup when SSH is not available yet. If Add-WindowsCapability fails (some Insider builds
  have no Feature-on-Demand source), install Win32-OpenSSH from its GitHub zip instead, as the
  README describes. ntdrive unloads PSReadLine per terminal session itself, so no profile change
  is needed here.
#>

[CmdletBinding()]
param(
  [switch]$Serial,
  [string]$HostIp,
  [int]$Port = 50000,
  [string]$Key
)

$ErrorActionPreference = "Stop"

Write-Host "== OpenSSH Server"
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
if ($cap.State -ne "Installed") {
  Add-WindowsCapability -Online -Name $cap.Name | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
    -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
Write-Host "  sshd running, default shell = powershell"

if ($Serial) {
  Write-Host "== kernel debugging over the serial pipe (COM1)"
  bcdedit /debug on | Out-Null
  bcdedit /dbgsettings serial debugport:1 baudrate:115200
  Write-Host "  reboot this guest, then kd_attach from the host"
} elseif ($HostIp) {
  Write-Host "== KDNET"
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

Write-Host "done"

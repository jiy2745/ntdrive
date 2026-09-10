<#
.SYNOPSIS
  Report everything ntdrive needs to know about a guest, in one run.

.DESCRIPTION
  Prints OS build, whether the current user is an administrator, Secure Boot state, the network
  adapter model (KDNET needs Intel e1000/e1000e), whether OpenSSH Server is installed and running,
  and the kdnet.exe view of supported NICs when kdnet.exe is present next to this script.
  Safe to run repeatedly. Intended to be launched through VMware Tools or SSH and its output
  captured to a file.
#>

$ErrorActionPreference = "Continue"

function Section($name) { Write-Output ""; Write-Output "== $name" }

Section "os"
$os = Get-CimInstance Win32_OperatingSystem
Write-Output ("caption=" + $os.Caption)
Write-Output ("build=" + $os.BuildNumber)
Write-Output ("arch=" + $os.OSArchitecture)

Section "identity"
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Output ("user=" + $id.Name)
Write-Output ("elevated=" + $isAdmin)

Section "secureboot"
try { Write-Output ("secureboot=" + (Confirm-SecureBootUEFI)) } catch { Write-Output ("secureboot=unknown (" + $_.Exception.Message + ")") }

Section "nic"
Get-NetAdapter | ForEach-Object {
  Write-Output ("adapter=" + $_.Name + " | " + $_.InterfaceDescription + " | " + $_.Status)
}
Get-PnpDevice -Class Net -PresentOnly -ErrorAction SilentlyContinue | ForEach-Object {
  Write-Output ("pnp=" + $_.FriendlyName + " | " + $_.InstanceId)
}

Section "openssh"
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue | Select-Object -First 1
Write-Output ("capability=" + ($cap.Name) + " state=" + ($cap.State))
$svc = Get-Service sshd -ErrorAction SilentlyContinue
if ($svc) { Write-Output ("sshd=" + $svc.Status + " startup=" + $svc.StartType) } else { Write-Output "sshd=absent" }
$shell = (Get-ItemProperty "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -ErrorAction SilentlyContinue).DefaultShell
Write-Output ("defaultshell=" + $shell)

Section "kdnet"
$bcd = bcdedit /dbgsettings 2>&1 | Out-String
Write-Output $bcd.Trim()
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$kdnet = Join-Path $here "kdnet.exe"
if (Test-Path $kdnet) {
  Write-Output "-- kdnet.exe supported NIC report:"
  & $kdnet 2>&1 | ForEach-Object { Write-Output $_ }
} else {
  Write-Output "kdnet.exe not present next to this script; skipping NIC support report"
}

Section "done"

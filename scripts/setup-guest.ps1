<#
.SYNOPSIS
  Prepare a Windows 10/11 guest for ntdrive. Run it inside the guest through setup-guest.cmd (any
  shell or a double click, whatever the execution policy says). It asks for administrator rights
  itself: a new window opens with those rights, does the work, shows the log and stays open until
  you close it.

.DESCRIPTION
  Creates a local administrator account for ntdrive (named ntdrive, -Account changes it, -NoAccount
  skips it and you use your own Windows account instead) and asks for its password, typed masked:
  SSH logs in with a Windows account and its password, and a personal account may have none or
  allow Windows Hello only. Type the same password in ntdrive setup on the host.

  -Standard also creates a second account without administrator rights (ntdrive-user,
  -StandardAccount changes the name) and asks for its password. ntdrive opens a terminal as it
  when asked (term_open account=standard), so the guest can be driven the way a plain user sees
  it: UAC prompts, access-denied paths, per-user settings. The administrator account stays,
  because ntdrive's own work (reading the KDNET key over SSH, file copies) needs it.

  Installs and starts OpenSSH Server with PowerShell as the default shell, opens port 22, and
  enables kernel debugging with bcdedit. By default that is KDNET: the host IP is inferred from the
  NAT gateway (x.x.x.2 means the host is x.x.x.1, -HostIp overrides), the key is generated in the
  guest and never needs copying, because the host reads it back over SSH (ntdrive verify, or the
  first kd_attach). The port comes from the machine id (50000-50039), so several guests of one host
  differ. -Serial sets up the serial named-pipe transport instead, -OpenSshOnly skips debugging.
  Reboot the guest afterwards, or let ntdrive verify on the host do it.

  OpenSSH comes from the Windows capability (Feature on Demand) when Windows can install it. When
  that fails, which is normal on Insider builds because Windows Update publishes no capability
  package for them, the script falls back to the Win32-OpenSSH zip: it downloads OpenSSH-Win64.zip
  from GitHub (or takes -OpenSshZip, a local path or URL), expands it to C:\Program Files\OpenSSH
  and runs its install-sshd.ps1. A guest that already has an sshd service keeps it, so the script
  is safe to run again.

  Output follows the ntdrive convention: `== n/total title` sections, OK / FAIL / WARN / INFO
  lines, `fix:` under a FAIL, and a DONE or NOT READY verdict with numbered next steps.

.EXAMPLE
  setup-guest.cmd
  The usual run: the ntdrive account, OpenSSH, KDNET. Then, on the host, ntdrive verify proves it.
  A plain .\setup-guest.ps1 is refused by the default execution policy, the .cmd is not.

.EXAMPLE
  setup-guest.cmd -Serial
  Serial named-pipe transport instead of KDNET.

.EXAMPLE
  setup-guest.cmd -NoAccount
  Use your own Windows account for SSH instead of creating the ntdrive account.

.EXAMPLE
  setup-guest.cmd -Standard
  Also create ntdrive-user, a plain account, for terminals opened as a standard user.
#>

[CmdletBinding()]
param(
  [string]$Account = "ntdrive",
  [switch]$NoAccount,
  [switch]$Standard,
  [string]$StandardAccount = "ntdrive-user",
  [switch]$Serial,
  [switch]$OpenSshOnly,
  [string]$HostIp,
  [int]$Port = 50000,
  [string]$Key,
  [string]$OpenSshZip = "https://github.com/PowerShell/Win32-OpenSSH/releases/latest/download/OpenSSH-Win64.zip",
  [switch]$Elevated
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# -- output convention (the same shapes as ntdrive setup and ntdrive verify) --------------------

function Step([int]$Index, [int]$Total, [string]$Title) { Write-Host "== $Index/$Total $Title" }
function Line([string]$Tag, [string]$Subject, [string]$Detail) {
  $text = "  " + $Tag.PadRight(5) + " " + $Subject
  if ($Detail) { $text += ": " + $Detail }
  Write-Host $text
}
function Ok([string]$Subject, [string]$Detail) { Line "OK" $Subject $Detail }
function Fail([string]$Subject, [string]$Detail) { Line "FAIL" $Subject $Detail }
function Warn([string]$Subject, [string]$Detail) { Line "WARN" $Subject $Detail }
function Info([string]$Subject, [string]$Detail) { Line "INFO" $Subject $Detail }
function Running([string]$Subject, [string]$Detail) { Line ".." $Subject $Detail }
function Fix([string]$Text) { Write-Host "        fix: $Text" }
function Verdict([string]$Word, [string]$Text) { Write-Host "${Word}: $Text" }
function NextSteps([string[]]$Steps) {
  Write-Host "  next:"
  $i = 1
  foreach ($step in $Steps) { Write-Host "    $i. $step"; $i++ }
}
function Mask([string]$Secret) {
  # Enough to recognize a password: the first two and the last character, stars between.
  $n = $Secret.Length
  if ($n -eq 0) { return "(empty)" }
  if ($n -le 3) { return $Secret.Substring(0, 1) + ("*" * ($n - 1)) + " ($n chars)" }
  return $Secret.Substring(0, 2) + ("*" * ($n - 3)) + $Secret.Substring($n - 1) + " ($n chars)"
}
function Plain([securestring]$Secure) {
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
function Read-AccountPassword([string]$Name) {
  # Typed masked (Read-Host -AsSecureString shows *), twice, echoed partly masked. This is the
  # Windows password of the local account, which is also what SSH checks.
  Info "account password" "the Windows password for $Name. Type the same one in ntdrive setup on the host"
  while ($true) {
    $first = Read-Host -AsSecureString "Password for $Name"
    $plain = Plain $first
    if (-not $plain) { Warn "password" "a password is needed, OpenSSH refuses empty ones"; continue }
    $second = Read-Host -AsSecureString "Repeat it"
    if ($plain -ne (Plain $second)) { Warn "password" "the two entries differ, try again"; continue }
    Info "entered" (Mask $plain)
    return $first
  }
}

# -- 1/4 administrator rights -----------------------------------------------------------------

Step 1 4 "Administrator rights"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  # Everything below needs administrator rights (a service, HKLM, a firewall rule, bcdedit).
  # Relaunch elevated in a visible window that shows the whole log and stays open at the end
  # (-Elevated makes it pause). One UAC click, and the window you read is the one doing the work.
  Info "needed for" "the OpenSSH service, the default shell (HKLM), the firewall rule and bcdedit"
  Info "opening an administrator window" "approve the UAC prompt. The setup runs and its log stays in that window"
  $forward = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Elevated")
  foreach ($bound in $PSBoundParameters.GetEnumerator()) {
    if ($bound.Value -is [switch]) {
      if ($bound.Value.IsPresent) { $forward += "-$($bound.Key)" }
    } else {
      $forward += @("-$($bound.Key)", "`"$($bound.Value)`"")
    }
  }
  try {
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $forward | Out-Null
  } catch {
    Fail "administrator rights" "the UAC prompt was refused"
    Fix "run setup-guest.cmd again and approve the prompt, or run it from an Administrator PowerShell"
    exit 1
  }
  exit 0
}
Ok "elevated" $identity.Name

# When this instance was relaunched with administrator rights, keep its window open at the end so
# the log can be read. The launching window has already closed.
function Complete([int]$Code) {
  if ($Elevated) {
    Write-Host ""
    Read-Host "Press Enter to close this window" | Out-Null
  }
  exit $Code
}

# -- helpers -------------------------------------------------------------------------------------

function Install-OpenSshCapability {
  # True when the Windows capability is installed (already, or by this call). False when Windows
  # cannot provide it: no OpenSSH.Server capability listed, or Add-WindowsCapability failed.
  try {
    $cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
  } catch {
    Warn "capability" "Get-WindowsCapability failed: $($_.Exception.Message)"
    return $false
  }
  if (-not $cap) {
    Warn "capability" "this build lists no OpenSSH.Server capability"
    return $false
  }
  if ($cap.State -eq "Installed") {
    Ok "capability" "$($cap.Name) already installed"
    return $true
  }
  try {
    Running "capability" "Add-WindowsCapability $($cap.Name), this asks Windows Update"
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    Ok "capability" "$($cap.Name) installed"
    return $true
  } catch {
    Warn "capability" "Add-WindowsCapability failed: $($_.Exception.Message)"
    return $false
  }
}

function Install-OpenSshZip([string]$Source) {
  # Win32-OpenSSH from its GitHub zip. Build independent, so it works where the capability does not.
  $dest = Join-Path $env:ProgramFiles "OpenSSH"
  $installer = Join-Path $dest "install-sshd.ps1"
  if (Test-Path $installer) {
    Ok "zip" "reusing the files already in $dest"
  } else {
    $work = Join-Path $env:TEMP "ntdrive-openssh"
    if (Test-Path $work) { Remove-Item $work -Recurse -Force }
    New-Item -ItemType Directory -Path $work | Out-Null
    $zip = Join-Path $work "OpenSSH-Win64.zip"
    if ($Source -match "^https?://") {
      Running "zip" "downloading $Source"
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
      Invoke-WebRequest -Uri $Source -OutFile $zip -UseBasicParsing
    } else {
      Info "zip" "using $Source"
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
    Ok "zip" "expanded to $dest"
  }
  # install-sshd.ps1 registers the sshd and ssh-agent services and fixes permissions. -Confirm:$false
  # keeps its ShouldProcess helpers from prompting.
  & $installer -Confirm:$false | Out-Null
  Ok "zip" "sshd and ssh-agent services registered"
}

function Assert-SecureBootOff {
  # bcdedit /debug on is refused while Secure Boot is on. BIOS firmware has no Secure Boot and
  # Confirm-SecureBootUEFI throws there, which means there is nothing to check.
  try { $on = Confirm-SecureBootUEFI } catch { return }
  if ($on) {
    throw "Secure Boot is on, so bcdedit /debug on would be refused. Power off the VM, turn Secure Boot off (VM settings > Options > Advanced), then run this script again."
  }
}

function Invoke-Bcdedit([string[]]$Arguments) {
  # Output stays hidden: for KDNET it carries the key, which the host reads over SSH later.
  $out = & bcdedit.exe @Arguments 2>&1
  if ($LASTEXITCODE -ne 0) {
    $shown = ($Arguments -join ' ') -replace 'key:\S+', 'key:***'
    throw "bcdedit $shown failed: $out"
  }
}

# -- 2/4 OpenSSH, 3/4 kernel debugging, 4/4 summary ---------------------------------------------

try {
  Step 2 4 "OpenSSH Server"
  if ($Account -and -not $NoAccount) {
    # A local administrator for ntdrive: SSH logs in with a Windows account and its password,
    # and a personal account may have no password or allow Windows Hello only.
    $secure = Read-AccountPassword $Account
    if (Get-LocalUser -Name $Account -ErrorAction SilentlyContinue) {
      Set-LocalUser -Name $Account -Password $secure -PasswordNeverExpires $true
      Ok "account" "$Account exists, password set"
    } else {
      New-LocalUser -Name $Account -Password $secure -PasswordNeverExpires -AccountNeverExpires `
        -Description "ntdrive: SSH and kernel debugging" | Out-Null
      Ok "account" "$Account created"
    }
    $admins = Get-LocalGroup -SID "S-1-5-32-544"
    if (-not (Get-LocalGroupMember -Group $admins -Member $Account -ErrorAction SilentlyContinue)) {
      Add-LocalGroupMember -Group $admins -Member $Account
    }
    Ok "account" "$Account is an administrator (bcdedit over SSH needs that)"
  }
  if ($Standard) {
    # A second account with no administrator rights, so the host can open a shell that sees the
    # guest the way a plain user does. Users group only: that is what grants it a logon.
    if (-not $NoAccount -and $StandardAccount -eq $Account) {
      throw "-StandardAccount $StandardAccount is the administrator account, pick another name"
    }
    $secureStandard = Read-AccountPassword $StandardAccount
    if (Get-LocalUser -Name $StandardAccount -ErrorAction SilentlyContinue) {
      Set-LocalUser -Name $StandardAccount -Password $secureStandard -PasswordNeverExpires $true
      Ok "standard account" "$StandardAccount exists, password set"
    } else {
      New-LocalUser -Name $StandardAccount -Password $secureStandard -PasswordNeverExpires `
        -AccountNeverExpires -Description "ntdrive: standard user for terminals" | Out-Null
      Ok "standard account" "$StandardAccount created"
    }
    $adminGroup = Get-LocalGroup -SID "S-1-5-32-544"
    if (Get-LocalGroupMember -Group $adminGroup -Member $StandardAccount -ErrorAction SilentlyContinue) {
      throw "$StandardAccount is an administrator and a standard account must not be: pick another name with -StandardAccount"
    }
    $userGroup = Get-LocalGroup -SID "S-1-5-32-545"
    if (-not (Get-LocalGroupMember -Group $userGroup -Member $StandardAccount -ErrorAction SilentlyContinue)) {
      Add-LocalGroupMember -Group $userGroup -Member $StandardAccount
    }
    Ok "standard account" "$StandardAccount is a plain user (Users group, no administrator rights)"
  }
  if (Get-Service sshd -ErrorAction SilentlyContinue) {
    Ok "sshd" "service already present, keeping it"
  } elseif (-not (Install-OpenSshCapability)) {
    Info "fallback" "the Win32-OpenSSH zip"
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
  Ok "sshd" "running ($sshdPath), default shell PowerShell, port 22 open"

  # A debugged or remotely driven VM must never sleep or hibernate: standby freezes the guest and
  # drops the SSH session, and hibernate tears down the KDNET or serial link.
  powercfg /change standby-timeout-ac 0 | Out-Null
  powercfg /change standby-timeout-dc 0 | Out-Null
  powercfg /change hibernate-timeout-ac 0 | Out-Null
  powercfg /change hibernate-timeout-dc 0 | Out-Null
  powercfg /hibernate off 2>$null | Out-Null
  Ok "power" "sleep and hibernate disabled, so the guest stays reachable"

  Step 3 4 "Kernel debugging"
  $kdSummary = ""
  if ($Serial) {
    Assert-SecureBootOff
    Invoke-Bcdedit @("/debug", "on")
    Invoke-Bcdedit @("/dbgsettings", "serial", "debugport:1", "baudrate:115200")
    Ok "serial" "COM1 named pipe, bcdedit written"
    $kdSummary = "serial"
  } elseif (-not $OpenSshOnly) {
    if (-not $HostIp) {
      # VMware NAT: the guest's gateway is x.x.x.2 and the host's VMnet8 adapter is x.x.x.1.
      $gateway = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric | Select-Object -First 1).NextHop
      if ("$gateway" -match '^(\d+\.\d+\.\d+)\.\d+$') { $HostIp = "$($Matches[1]).1" }
    }
    if (-not $HostIp) {
      Warn "kdnet" "no default gateway, so the host IP is unknown here"
      Fix "nothing to do now: ntdrive verify on the host configures KDNET over SSH"
    } else {
      if (-not $PSBoundParameters.ContainsKey("Port")) {
        # Each guest picks its own port in 50000-50039 from its machine id, so several guests of
        # one host rarely collide. The host moves a colliding guest when it reads the settings.
        $guid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -ErrorAction SilentlyContinue).MachineGuid
        $sum = 0
        foreach ($ch in [char[]]"$guid") { $sum = ($sum * 31 + [int]$ch) % 1000003 }
        $Port = 50000 + ($sum % 40)
      }
      Assert-SecureBootOff
      Invoke-Bcdedit @("/debug", "on")
      $net = @("/dbgsettings", "net", "hostip:$HostIp", "port:$Port")
      if ($Key) { $net += "key:$Key" }
      Invoke-Bcdedit $net
      Ok "kdnet" "host $HostIp port $Port, key generated here (the host reads it over SSH)"
      $kdSummary = "kdnet"
    }
  } else {
    Info "skipped" "-OpenSshOnly, ntdrive verify on the host can configure the debugger over SSH"
  }

  Step 4 4 "Summary"
  $user = $identity.Name.Split('\')[-1]
  $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -ExpandProperty IPAddress
  $listening = [bool](Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue)
  if ($Account -and -not $NoAccount) {
    Info "guest account" "$Account (use this name and the password you just typed in ntdrive setup on the host)"
  } else {
    Info "guest account" "$user (use this name and its Windows password in ntdrive setup on the host)"
    Info "no password or Windows Hello only?" "run setup-guest.cmd without -NoAccount to get a local administrator for ntdrive"
  }
  if ($Standard) {
    Info "standard account" "$StandardAccount (a plain user: at the standard account prompt of ntdrive setup on the host type this name and its password)"
  }
  Info "guest IPv4" "$($ips -join ', ') (ntdrive finds it through VMware Tools, this is for a manual ssh test)"
  if ($listening) { Ok "sshd listening" "port 22" } else { Warn "sshd listening" "port 22 is not listening yet" }
  $steps = @()
  if ($kdSummary) {
    Verdict "DONE" "OpenSSH and the $kdSummary debugger are configured in this guest"
    $steps += "reboot this guest so it boots with the debugger on (ntdrive verify can do it for you)"
  } else {
    Verdict "DONE" "OpenSSH is configured in this guest, the debugger is not yet"
  }
  $steps += "on the host run ntdrive verify (or scripts\setup-host.cmd -Verify): it reads the KDNET key, reboots if needed and ends with ALL SET"
  NextSteps $steps
  Complete 0
} catch {
  Fail "setup" $_.Exception.Message
  Fix "fix the cause above, then run setup-guest.cmd again (it skips what is already done)"
  Verdict "NOT READY" "this guest is not set up yet"
  Complete 1
}

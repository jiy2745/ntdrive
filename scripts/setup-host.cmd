@echo off
rem Runs setup-host.ps1 from any shell or by double click, whatever the PowerShell execution
rem policy says. Arguments pass through, for example: setup-host.cmd -FirewallOnly
rem A double click closes the window at the end and the log is lost, so keep it open then. When
rem started from a shell (cmdcmdline does not name this script) it returns without pausing.
echo %cmdcmdline% | find /i "%~nx0" >nul && set "_ntdrive_pause=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-host.ps1" %*
set "_ntdrive_code=%errorlevel%"
if defined _ntdrive_pause pause
exit /b %_ntdrive_code%

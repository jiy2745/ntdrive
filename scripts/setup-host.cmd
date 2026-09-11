@echo off
rem Runs setup-host.ps1 from any shell or by double click, whatever the PowerShell execution
rem policy says. Arguments pass through, for example: setup-host.cmd -FirewallOnly
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-host.ps1" %*
if errorlevel 1 pause

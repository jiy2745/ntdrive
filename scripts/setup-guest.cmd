@echo off
rem Runs setup-guest.ps1 from any shell or by double click, whatever the PowerShell execution
rem policy says (a plain .\setup-guest.ps1 is refused on a default Windows install). The script
rem asks for administrator rights itself and does its work in that elevated window, which stays
rem open until you close it. Arguments pass through, for example: setup-guest.cmd -Serial
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-guest.ps1" %*
if errorlevel 1 pause

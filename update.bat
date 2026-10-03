@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\update-windows.ps1"
if errorlevel 1 echo Update failed. See the message above. Your data and secrets remain in place.
pause

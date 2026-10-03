@echo off
setlocal
cd /d "%~dp0"
echo Marketing Manager - private Windows installation
where py >nul 2>nul
if errorlevel 1 goto missing_python
py -3.12 -c "import sys; assert sys.version_info[:2] == (3,12)" >nul 2>nul
if errorlevel 1 goto missing_python
goto python_ready
:missing_python
echo Python 3.12 is required. No existing Python installation will be changed.
choice /C YN /M "Install Python 3.12 with winget"
if errorlevel 2 goto failed
winget install --id Python.Python.3.12 --exact --scope user --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto failed
echo Close this window and double-click install.bat again so the Python launcher is available.
pause
exit /b 0
:python_ready
if exist ".venv\Scripts\python.exe" goto check_venv
py -3.12 -m venv .venv
if errorlevel 1 goto failed
:check_venv
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info[:2] == (3,12)" >nul 2>nul
if errorlevel 1 (
  echo The existing .venv uses a different Python. Rename .venv and rerun this installer.
  goto failed
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.playwright"
".venv\Scripts\python.exe" -m playwright install chromium --only-shell
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m bot.fonts
if errorlevel 1 goto failed
if not exist secrets.txt (
  copy secrets.example.txt secrets.txt >nul
  echo Fill secrets.txt using README.md. Save and close Notepad to continue.
  start /wait notepad.exe "%CD%\secrets.txt"
)
".venv\Scripts\python.exe" -m bot.setup
if errorlevel 1 goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\install-windows.ps1"
if errorlevel 1 goto failed
echo Setup complete. Double-click Marketing Manager on your desktop.
echo Keep DRY_RUN=true until you have reviewed the sample designs.
pause
exit /b 0
:failed
echo Installation is incomplete. Fix the error above and double-click install.bat again.
echo No trading bot or other process has been stopped or changed.
pause
exit /b 1

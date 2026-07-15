@echo off
:: ============================================================
::  INCAZ one-click installer (Windows)
::  Creates a private Python environment, installs INCAZ and
::  puts an INCAZ shortcut on your desktop. No admin needed.
:: ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
echo.
echo  INCAZ installer
echo  ===============
echo.

:: ---- find Python 3.10+ ------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo  [ERROR] Python was not found.
    echo.
    echo  Please install Python 3.10 or newer from:
    echo      https://www.python.org/downloads/
    echo  During installation tick "Add python.exe to PATH",
    echo  then run this installer again.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo  Found Python !PYVER!

:: ---- create environment -----------------------------------
echo  Creating environment (.venv) ...
%PY% -m venv .venv
if errorlevel 1 (
    echo  [ERROR] Could not create the Python environment.
    pause
    exit /b 1
)

echo  Installing INCAZ and its dependencies (few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install . --quiet
if errorlevel 1 (
    echo  [ERROR] Installation failed - check your internet connection.
    pause
    exit /b 1
)

:: ---- desktop shortcut --------------------------------------
echo  Creating desktop shortcut ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\INCAZ.lnk');" ^
  "$lnk.TargetPath = '%CD%\.venv\Scripts\incaz.exe';" ^
  "$lnk.WorkingDirectory = '%CD%';" ^
  "$lnk.IconLocation = '%CD%\incaz\assets\incaz.ico';" ^
  "$lnk.Description = 'INCAZ - INtegrated Calibration & Acquisition, Zero-cost';" ^
  "$lnk.Save()"

echo.
echo  Done! Start INCAZ from the desktop shortcut
echo  (or run: .venv\Scripts\incaz.exe)
echo.
echo  Optional: try it without hardware first -
echo  double-click installer\start_demo_ecu.bat to run a
echo  simulated ECU, then use demo\demo.a2l + demo\demo.hex.
echo.
pause

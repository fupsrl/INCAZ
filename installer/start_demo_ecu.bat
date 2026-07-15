@echo off
:: Starts the simulated demo ECU (XCP on UDP+TCP, port 5555).
:: Use demo\demo.a2l and demo\demo.hex in INCAZ to talk to it.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Run installer\install.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m incaz.sim --hex demo\demo.hex
pause

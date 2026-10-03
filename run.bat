@echo off
rem Launches with no console window. If nothing appears, use run-debug.bat.
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo.
  echo Not set up yet. Run setup.bat first ^(one time only^).
  echo.
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "src\main.py" %*

@echo off
rem Same app, but with a console so you can watch the output.
rem Use this if run.bat seems to do nothing.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Not set up yet. Run setup.bat first ^(one time only^).
  echo.
  pause
  exit /b 1
)

.venv\Scripts\python.exe "src\main.py" %*
echo.
echo App exited. Log: config\sniper.log
pause

@echo off
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please install the Windows dependencies described in README.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" voice_app.py
pause

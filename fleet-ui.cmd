@echo off
rem FLU fleet: double-click to open the UGV setup app in the browser (http://127.0.0.1:8765).
rem   - not installed yet: runs setup.cmd (Python, WireGuard, .venv, shortcut);
rem   - pyproject.toml changed since the last install (e.g. after git pull): updates dependencies;
rem   - already running: only opens the browser, a second copy is not started.
title FLU fleet
cd /d "%~dp0"

if not exist ".venv\Scripts\fleet.exe" (
  call "%~dp0setup.cmd"
  exit /b
)

fc /b pyproject.toml ".venv\pyproject.installed" >nul 2>&1 || (
  echo Updating FLU fleet dependencies...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e . && copy /y pyproject.toml ".venv\pyproject.installed" >nul
)

".venv\Scripts\fleet.exe" ui
if errorlevel 1 pause

@echo off
rem UGV Quick Setup: double-click to open the UGV setup app in its own window (no browser).
rem   - not installed yet: runs setup.cmd (Python, WireGuard, .venv, shortcut);
rem   - pyproject.toml changed since the last install (e.g. after git pull): updates dependencies;
rem   - already running: opens one more window to the same wizard.
rem The browser version is still there: .venv\Scripts\fleet.exe ui
title UGV Quick Setup
cd /d "%~dp0"

if not exist ".venv\Scripts\fleet.exe" (
  call "%~dp0setup.cmd"
  exit /b
)

fc /b pyproject.toml ".venv\pyproject.installed" >nul 2>&1 || (
  echo Updating UGV Quick Setup dependencies...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e . && copy /y pyproject.toml ".venv\pyproject.installed" >nul
)

rem pythonw: no console window; if something goes wrong, the app shows a dialog
start "" ".venv\Scripts\pythonw.exe" -m fleet.app

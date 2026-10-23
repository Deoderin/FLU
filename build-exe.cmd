@echo off
rem UGV Quick Setup: build the standalone app (no Python needed on the target laptop).
rem Result: dist\UGV Quick Setup\ (UGV Quick Setup.exe, fleet.exe, data) and a zip next to it.
rem Only templates go into the build: this laptop's inventory\ and secrets\ stay here.
title UGV Quick Setup - build
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.cmd first: it creates .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e ".[build]" || goto :fail
".venv\Scripts\python.exe" packaging\build.py || goto :fail
explorer dist
exit /b 0
:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1

@echo off
rem FLU fleet: installer for a laptop. Double-click to run.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause

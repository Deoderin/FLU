@echo off
rem FLU fleet: local web UI (http://127.0.0.1:8765)
cd /d "%~dp0"
".venv\Scripts\fleet.exe" ui

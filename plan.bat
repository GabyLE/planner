@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo planner venv missing - run planner\build first 1>&2
    exit /b 1
)
".venv\Scripts\python.exe" -m logistics_planner plan %*
exit /b %errorlevel%

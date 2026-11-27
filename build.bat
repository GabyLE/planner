@echo off
setlocal
cd /d "%~dp0"

set "VENV=.venv"
if exist "%VENV%\Scripts\python.exe" goto have_venv

set "PY=py -3"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 set "PY=python"
%PY% -m venv "%VENV%"
if errorlevel 1 exit /b 1

:have_venv
"%VENV%\Scripts\python.exe" -c "import pymysql" >nul 2>&1
if errorlevel 1 (
    "%VENV%\Scripts\python.exe" -m pip install pymysql
    if errorlevel 1 exit /b 1
)
"%VENV%\Scripts\python.exe" -c "import pytest" >nul 2>&1
if errorlevel 1 (
    "%VENV%\Scripts\python.exe" -m pip install pytest
    if errorlevel 1 exit /b 1
)

"%VENV%\Scripts\python.exe" -m pytest tests -q
if errorlevel 1 exit /b 1

echo planner ready: %CD%\%VENV%\Scripts\python.exe -m logistics_planner

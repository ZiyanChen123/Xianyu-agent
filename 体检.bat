@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0doctor.ps1"
pause

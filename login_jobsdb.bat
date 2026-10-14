@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv_test\Scripts\python.exe" run.py login jobsdb
pause

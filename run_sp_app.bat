@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0launch_hold_only.py" %*
) else (
    python "%~dp0launch_hold_only.py" %*
)
exit /b %errorlevel%

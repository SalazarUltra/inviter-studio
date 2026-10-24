@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "INVITER_PY=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "INVITER_PY=venv\Scripts\python.exe"
) else (
    echo Virtual environment not found. Run: py -3.14 -m venv .venv
    echo Then: .venv\Scripts\python.exe -m pip install -r requirements-lock.txt
    pause
    exit /b 1
)
"%INVITER_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Python 3.11+ is required. Recreate .venv using Python 3.14; see README.md.
    pause
    exit /b 1
)
"%INVITER_PY%" main.py
pause


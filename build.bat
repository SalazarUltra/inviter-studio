@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "INVITER_PY=.venv\Scripts\python.exe"
) else (
    set "INVITER_PY=venv\Scripts\python.exe"
)
"%INVITER_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Python 3.11+ is required. Recreate .venv using Python 3.14; see README.md.
    exit /b 1
)
"%INVITER_PY%" -m pip install -r requirements-lock.txt -r requirements-build.txt
if errorlevel 1 exit /b 1
"%INVITER_PY%" -m tools.build_windows
if errorlevel 1 exit /b 1
echo Application and installer are in dist.

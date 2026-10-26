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
"%INVITER_PY%" -m pip install -r requirements-lock.txt -r requirements-build.txt ruff==0.17.0
if errorlevel 1 exit /b 1
rem Quality gate: lint, formatting, compileall, unit tests and pip check must
rem pass before a distributable build is produced.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\check_quality.ps1"
if errorlevel 1 exit /b 1
"%INVITER_PY%" -m tools.build_windows
if errorlevel 1 exit /b 1
echo Application and portable package are in dist.

@echo off
cd /d "%~dp0"
if exist "dist\StartInviterStudio.bat" (
    call "dist\StartInviterStudio.bat"
    exit /b
)
set "INVITER_HOME=%~dp0"
start "" "dist\InviterStudio\InviterStudio.exe"

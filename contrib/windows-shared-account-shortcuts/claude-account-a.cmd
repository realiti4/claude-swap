@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--check" goto check
if /i "%~1"=="--help" goto help
"%~dp0venv\Scripts\python.exe" -B "%~dp0slash\prepare_launch.py" 1
if errorlevel 1 exit /b %ERRORLEVEL%
call claude %*
exit /b %ERRORLEVEL%

:check
"%~dp0venv\Scripts\python.exe" -B "%~dp0slash\prepare_launch.py" 1 --check
exit /b %ERRORLEVEL%

:help
echo Select shared default Account 1 / account-a, then start Claude Code.
echo Existing default-profile Claude windows follow the selected account.
echo Usage: claude-account-a [Claude arguments]
echo Examples: claude-account-a --resume
echo Checks only: claude-account-a --check
exit /b 0

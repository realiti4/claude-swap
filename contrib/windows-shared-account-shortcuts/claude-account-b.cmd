@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--check" goto check
if /i "%~1"=="--help" goto help
"%~dp0venv\Scripts\python.exe" -B "%~dp0slash\prepare_launch.py" 2
if errorlevel 1 exit /b %ERRORLEVEL%
call claude %*
exit /b %ERRORLEVEL%

:check
"%~dp0venv\Scripts\python.exe" -B "%~dp0slash\prepare_launch.py" 2 --check
exit /b %ERRORLEVEL%

:help
echo Select shared default Account 2 / account-b, then start Claude Code.
echo Existing default-profile Claude windows follow the selected account.
echo Usage: claude-account-b [Claude arguments]
echo Examples: claude-account-b --resume
echo Checks only: claude-account-b --check
exit /b 0

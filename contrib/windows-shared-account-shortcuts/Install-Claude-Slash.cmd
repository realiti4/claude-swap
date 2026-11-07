@echo off
setlocal
if /i "%~1"=="--check" goto check
if /i "%~1"=="--uninstall" goto uninstall
if not "%~1"=="" goto help
"%~dp0venv\Scripts\python.exe" -B "%~dp0slash\install_slash.py" --install
exit /b %ERRORLEVEL%

:check
"%~dp0venv\Scripts\python.exe" -B "%~dp0slash\install_slash.py" --check
exit /b %ERRORLEVEL%

:uninstall
"%~dp0venv\Scripts\python.exe" -B "%~dp0slash\install_slash.py" --uninstall
exit /b %ERRORLEVEL%

:help
echo Usage: Install-Claude-Slash.cmd [--check^|--uninstall]
exit /b 0

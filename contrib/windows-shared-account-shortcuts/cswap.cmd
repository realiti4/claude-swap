@echo off
setlocal
set "PYTHONDONTWRITEBYTECODE=1"
"%~dp0venv\Scripts\cswap.exe" %*
set "CSWAP_RESULT=%ERRORLEVEL%"
endlocal & exit /b %CSWAP_RESULT%

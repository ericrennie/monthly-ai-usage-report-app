@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 scripts\report_app.py
  goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
  python scripts\report_app.py
  goto :eof
)
echo Python 3.11 or newer is required.
echo Install it from https://www.python.org/downloads/ and select "Add Python to PATH".
pause

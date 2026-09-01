@echo off
REM Double-click this to open the seocrawl UI in your browser.
REM Reports are written into the "reports" folder next to this file.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   The virtual environment is missing. Creating it now...
  echo.
  py -3 -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e .
)

if not exist "reports" mkdir "reports"

echo.
echo   Starting seocrawl. Your browser will open in a moment.
echo   Close this window to stop the server.
echo.

".venv\Scripts\python.exe" -m seocrawl ui --output-dir "reports"
goto :eof

:nopython
echo.
echo   Python 3.11 or newer was not found.
echo   Install it from https://www.python.org/downloads/ and run this again.
echo.
pause

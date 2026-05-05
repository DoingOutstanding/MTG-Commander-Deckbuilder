@echo off
REM ---------------------------------------------------------------------
REM  MTG Commander Deckbuilder — one-click launcher (Windows)
REM
REM  Double-click this file to start the deckbuilder.  It will:
REM    1. Verify Python is installed
REM    2. Install Flask if it isn't already
REM    3. Build cards.jsonl from the Scryfall oracle-cards JSON
REM       (only if cards.jsonl doesn't already exist or is missing)
REM    4. Start the local web server
REM    5. Open your default browser to http://127.0.0.1:5000
REM
REM  Close this command window to stop the server.
REM ---------------------------------------------------------------------

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   MTG Commander Deckbuilder
echo ============================================================
echo.

REM Find a Python interpreter — try `py` (Windows launcher) first,
REM then `python`, then `python3`.
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where python3 >nul 2>nul && set "PY=python3"

if not defined PY (
    echo [ERROR] Python is not installed or not on PATH.
    echo.
    echo Please install Python 3.10 or newer from:
    echo     https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Using Python: %PY%
%PY% --version

REM Make sure Flask is available; install it quietly if not.
%PY% -c "import flask" 2>nul
if errorlevel 1 (
    echo.
    echo Flask is not installed.  Installing now...
    %PY% -m pip install --quiet flask
    if errorlevel 1 (
        echo [ERROR] Failed to install Flask.  Check your internet connection.
        pause
        exit /b 1
    )
    echo Flask installed.
)

REM Build cards.jsonl if it's missing.  profile.py will print a clear
REM error if it can't locate the oracle-cards-*.json file.
if not exist "cards.jsonl" (
    echo.
    echo cards.jsonl not found.  Building it from oracle-cards-*.json...
    %PY% profile.py
    if errorlevel 1 (
        echo.
        echo [ERROR] Couldn't build cards.jsonl.
        echo Make sure an oracle-cards-*.json file from Scryfall is in this folder
        echo or in your Downloads / Desktop.  Download it from:
        echo     https://scryfall.com/docs/api/bulk-data
        echo and pick "Oracle Cards".
        pause
        exit /b 1
    )
)

REM Open the browser ~3 seconds after launching the server.
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:5000"

echo.
echo Starting server at http://127.0.0.1:5000
echo Close this window to stop the deckbuilder.
echo.
%PY% app.py

endlocal

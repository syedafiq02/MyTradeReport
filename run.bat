@echo off
REM MT5 Live Personal Dashboard - Windows launcher
REM Usage:  run.bat          (live MT5 data)
REM         run.bat demo     (synthetic DEMO data, separate database)
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "APP_ARGS=--open"
if /I "%~1"=="demo" set "APP_ARGS=--open --demo"

REM 1. Find Python (prefer the py launcher)
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3 was not found. Install it from https://www.python.org/downloads/windows/
    echo         and tick "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)

REM 2. Create the virtual environment if missing
if not exist "%VENV_PY%" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

REM 3. Install requirements when requirements.txt changed since the last install
set "STAMP=.venv\requirements.installed"
set "NEED_INSTALL=1"
if exist "%STAMP%" (
    fc /b requirements.txt "%STAMP%" >nul 2>nul
    if not errorlevel 1 set "NEED_INSTALL=0"
)
if "%NEED_INSTALL%"=="1" (
    echo Installing requirements ...
    "%VENV_PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Installing requirements failed. See the messages above.
        pause
        exit /b 1
    )
    copy /y requirements.txt "%STAMP%" >nul
)

REM 4. Create .env from the template on first run (contains no secrets)
if not exist ".env" (
    copy /y .env.example .env >nul
    echo Created .env from .env.example - edit it to set MT5_PATH or a dashboard password.
)

REM 5. Start the dashboard (it opens http://127.0.0.1:5000 in your browser)
echo.
echo Starting MT5 Live Dashboard - press Ctrl+C to stop.
"%VENV_PY%" app.py %APP_ARGS%
echo.
echo The dashboard has stopped.
pause
endlocal

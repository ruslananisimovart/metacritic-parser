@echo off
rem ===========================================================================
rem  Metacritic parser - service control.
rem
rem  This file stays pure ASCII on purpose: cmd.exe reads batch files by byte
rem  offset, so a code page switch plus non-ASCII text desyncs the parser and
rem  corrupts the script. All UI text lives in tools\service_control.py, which
rem  switches the console to UTF-8 by itself.
rem
rem  Double click: menu - 1 start, 2 restart, 3 stop, 0 exit.
rem  Command line: Metacritic_Parser.bat start ^| restart ^| stop ^| status
rem
rem  Nothing is installed: no Windows service, no scheduled task, no autostart.
rem  Default port is 8000, override with: set SERVICE_PORT=8010
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Metacritic parser

set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "CONTROL=%~dp0tools\service_control.py"
if not exist "%CONTROL%" goto :no_control

rem interpreter lookup: project virtual environment, then PATH, then the py launcher
set "PYEXE="
set "PYARGS="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0venv\Scripts\python.exe" set "PYEXE=%~dp0venv\Scripts\python.exe"
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARGS=-3"
    )
)
if not defined PYEXE goto :no_python

"%PYEXE%" %PYARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python

"%PYEXE%" %PYARGS% "%CONTROL%" %*
set "CODE=%ERRORLEVEL%"
rem started by double click and something went wrong: keep the window open
if not "%CODE%"=="0" if "%~1"=="" pause
endlocal & exit /b %CODE%

:no_control
echo.
echo   tools\service_control.py not found next to this file.
echo   Keep %~nx0 inside the project folder.
echo.
pause
endlocal & exit /b 1

:no_python
echo.
echo   Python 3.12 not found.
echo   Install it from python.org, then run: pip install -r requirements.txt
echo.
pause
endlocal & exit /b 1

:old_python
echo.
echo   Python 3.12 or newer is required, found an older one: %PYEXE% %PYARGS%
echo   Install Python 3.12 or put it into the project .venv folder.
echo.
pause
endlocal & exit /b 1

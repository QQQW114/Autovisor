@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title ZhiHuiShu Auto Study
cd /d "%~dp0"

echo ============================================
echo   ZhiHuiShu Auto Study
echo ============================================
echo.

echo [1/4] Cleaning leftover processes...
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM Autovisor.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/4] Checking environment...
if not exist "%~dp0.venv\Scripts\python.exe" goto NOVENV
echo       OK

echo [3/4] Checking account and course info...
"%~dp0.venv\Scripts\python.exe" "%~dp0setup_wizard.py"
if errorlevel 1 goto SETUPFAIL
if not exist "%~dp0config.ini" goto NOCONFIG

echo [4/4] Starting...
echo.
echo --------------------------------------------
echo   Browser will open and log in automatically.
echo   If a captcha appears, the program tries to
echo   solve it up to 3 times, then waits for you.
echo   Do NOT close or minimize the browser window.
echo.
echo   Press Ctrl+C in this window to stop.
echo --------------------------------------------
echo.

"%~dp0.venv\Scripts\python.exe" "%~dp0Autovisor.py"
set EXITCODE=%ERRORLEVEL%

echo.
echo ============================================
echo   Program exited. Code = %EXITCODE%
echo ============================================
pause
exit /b %EXITCODE%

:NOVENV
echo.
echo   [ERROR] Python env not found:
echo           %~dp0.venv\Scripts\python.exe
echo.
echo   Install dependencies first:
echo     uv venv .venv --python 3.13
echo     uv pip install --python .venv\Scripts\python.exe httpx pillow playwright pygetwindow requests numpy opencv-python ddddocr
echo.
echo   (or see README "Deployment" section)
echo.
pause
exit /b 1

:NOCONFIG
echo.
echo   [ERROR] config.ini not found in:
echo           %~dp0
echo.
pause
exit /b 1

:SETUPFAIL
echo.
echo   [ERROR] Account setup was cancelled or failed.
echo.
pause
exit /b 1

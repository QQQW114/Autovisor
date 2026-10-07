@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Change Course
cd /d "%~dp0"

echo ============================================
echo   Change Course / Account
echo ============================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" goto NOVENV

echo   Current settings:
echo --------------------------------------------
"%~dp0.venv\Scripts\python.exe" "%~dp0setup_wizard.py" --show
echo --------------------------------------------
echo.

echo   [1] Change course URL
echo   [2] Change account
echo   [3] Change both
echo   [0] Exit
echo.
set /p CHOICE=  Choose:

if "%CHOICE%"=="1" goto COURSE
if "%CHOICE%"=="2" goto ACCOUNT
if "%CHOICE%"=="3" goto BOTH
goto END

:COURSE
"%~dp0.venv\Scripts\python.exe" "%~dp0setup_wizard.py" --course
goto END

:ACCOUNT
"%~dp0.venv\Scripts\python.exe" "%~dp0setup_wizard.py" --account
goto END

:BOTH
"%~dp0.venv\Scripts\python.exe" "%~dp0setup_wizard.py" --all
goto END

:NOVENV
echo   [ERROR] Python env not found. Run the launcher bat first to see setup steps.
echo.

:END
echo.
echo   Done. Restart the launcher bat to apply.
echo.
pause

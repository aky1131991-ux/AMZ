@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment. Install Python from python.org and try again.
        pause
        exit /b 1
    )
)

echo Installing required packages...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo Installing Playwright browser...
.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
    echo Playwright browser installation failed.
    pause
    exit /b 1
)

echo Starting Amazon Shift Finder...
.venv\Scripts\python.exe app.py
pause
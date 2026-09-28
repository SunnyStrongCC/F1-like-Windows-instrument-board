@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   F1 Telemetry Dashboard - install deps
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    echo         Install Python 3.10+ first: https://www.python.org/downloads/
    pause
    exit /b 1
)

python -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] pip install failed. Check your network / proxy.
    pause
    exit /b 1
)

echo.
echo Done. Now double-click "启动仪表盘.bat" to start.
pause

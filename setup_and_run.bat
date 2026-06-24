@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set CACHE_FILE=%~dp0.setup_done

if exist "%CACHE_FILE%" (
    echo.
    echo  [SKIP] Setup already done. Starting app...
    echo         To re-run setup, delete .setup_done
    echo.
    goto LAUNCH
)

echo.
echo ============================================================
echo   Arca Live Downloader - Setup and Run
echo ============================================================
echo.

echo [1/4] Checking Python...
python --version > nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Python not found.
    echo          Install Python 3.11+ from https://www.python.org/downloads/
    echo          Enable "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%V in ('python --version 2^>^&1') do set PYVER=%%V
echo  Python %PYVER% OK
echo.

echo [2/4] Upgrading pip...
python -m pip install --upgrade pip --quiet
echo  pip OK
echo.

echo [3/4] Installing packages...
python -m pip install requests beautifulsoup4 lxml pillow selenium webdriver-manager --quiet --no-warn-script-location
if errorlevel 1 (
    echo.
    echo  [ERROR] Package install failed. Check internet or run as Administrator.
    pause
    exit /b 1
)
echo  Packages OK
echo.

echo [4/4] Downloading Edge WebDriver...
python "%~dp0_install_edgedriver.py" "%~dp0"
if errorlevel 1 (
    echo.
    echo  [WARNING] Edge WebDriver install failed.
    echo            Login feature may not work.
    echo            Press any key to continue...
    pause
)
echo.

echo setup_done > "%CACHE_FILE%"
echo  Cache saved. Next launch will skip setup.
echo.

echo ============================================================
echo   Ready! Launching app...
echo ============================================================
echo.

:LAUNCH
python "%~dp0arca_gui.py"

if errorlevel 1 (
    echo  [ERROR] Program exited with an error.
    pause
)

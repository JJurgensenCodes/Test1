@echo off
REM Builds CryptoLounge.exe (one file) for Windows.
REM Run this from the project folder: build_windows.bat
setlocal

echo Checking Python...
python --version
if errorlevel 1 (
    echo.
    echo ERROR: "python" was not found on PATH. Install Python 3.10+ from
    echo python.org and make sure to check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

echo.
echo Installing requirements...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed -- see the message above. The exe was NOT built.
    pause
    exit /b 1
)

echo.
echo Building CryptoLounge.exe with PyInstaller...
python -m PyInstaller --noconfirm --onefile --console --name CryptoLounge --add-data "static;static" desktop_launcher.py
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed -- see the message above. No dist\ folder was created.
    pause
    exit /b 1
)

if not exist "dist\CryptoLounge.exe" (
    echo.
    echo ERROR: Build finished but dist\CryptoLounge.exe was not found.
    echo Check the PyInstaller output above for warnings.
    pause
    exit /b 1
)

echo.
echo SUCCESS. CryptoLounge.exe is in the dist\ folder.
echo Double-click it to run -- it opens in your default browser.
pause

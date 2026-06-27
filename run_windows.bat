@echo off
REM Quickly runs the latest code for testing -- no exe build, opens in your
REM default browser. Use build_windows.bat when you want a real CryptoLounge.exe.
setlocal

python --version
if errorlevel 1 (
    echo ERROR: "python" was not found on PATH. Install Python 3.10+ from python.org.
    pause
    exit /b 1
)

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed -- see the message above.
    pause
    exit /b 1
)

python webapp.py
pause

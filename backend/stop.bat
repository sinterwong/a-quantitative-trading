@echo off
:: stop.bat — Stop the Portfolio Backend Service
:: Finds the Python process running main.py and kills it

echo Searching for backend process...

for /f "tokens=5" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| findstr /i "main.py"') do (
    echo Killing process: %%a
    taskkill /PID %%a /F >nul 2>&1
    echo Stopped.
    goto :done
)

echo No backend process found (may already be stopped).
:done
pause

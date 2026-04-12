@echo off
:: status.bat — Check if Portfolio Backend is running

for /f "tokens=5" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| findstr /i "main.py"') do (
    echo [RUNNING] Backend process PID: %%a
    goto :check_api
)

echo [STOPPED] No backend process found.
goto :end

:check_api
curl -s --max-time 2 http://127.0.0.1:5555/health >nul 2>&1
if %errorlevel%==0 (
    echo [OK] API responding on http://127.0.0.1:5555
) else (
    echo [WARN] Process running but API not responding
)

:end
echo.
echo API docs: http://127.0.0.1:5555
pause

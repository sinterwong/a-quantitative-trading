@echo off
:: start.bat — Windows startup script for Portfolio Backend Service
:: Usage: double-click or: start.bat

cd /d "%~dp0"
echo Starting Portfolio Backend Service...
start "Portfolio Backend" cmd /k "python main.py --mode both --port 5555"
echo Service started on http://127.0.0.1:5555
echo API docs: http://127.0.0.1:5555
pause

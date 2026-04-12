@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python "C:\Users\sinte\.openclaw\workspace\scripts\quant\main.py" --symbols 600900.SH 512690.SH 159992.SZ --start 20200101 --end 20251231 --feishu --output "C:\Users\sinte\.openclaw\workspace\scripts\quant\report_full.md"

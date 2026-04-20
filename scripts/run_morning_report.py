import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import subprocess
result = subprocess.run(['python', r'C:\Users\sinte\.openclaw\workspace\quant_repo\scripts\morning_report.py'], 
    capture_output=False)
sys.exit(result.returncode)

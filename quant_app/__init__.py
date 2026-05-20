"""
quant_app — 启动器包（P3-2）

按 mode 装配的进程入口:
  - serve_api.py — Flask API HTTP server 进程
  - run_worker.py — Scheduler + IntradayMonitor + StrategyRunner
  - main.py — 按 mode (all / api / worker) 装配启动器

启动命令: `python -m quant_app.main --mode all`
（R2-2: backend/main.py 旧 shim 已删除，所有调用方直接走 quant_app。）
"""

"""
quant_app — 启动器包（P3-2）

把原 backend/main.py 的 API/Worker 启动逻辑解耦为 3 个模块:
  - serve_api.py — Flask API HTTP server 进程
  - run_worker.py — Scheduler + IntradayMonitor + StrategyRunner
  - main.py — 按 mode (all / api / worker) 装配启动器

backend/main.py 保留为薄壳 shim,转发到本包,保证既有调用入口/测试兼容。
"""

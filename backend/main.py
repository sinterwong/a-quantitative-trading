"""
backend/main.py — Backward-compat shim (P3-2)
=============================================

实际入口已搬到 ``quant_app/main.py``。本文件保留原有调用面用作兼容:

  - ``python backend/main.py``        ↔  ``python -m quant_app.main``
  - ``from backend.main import Scheduler, get_monitor, get_broker, …``
  - ``import backend.main as bm; bm._trade_calendar = set()``  ← 测试中使用

模块级名字通过下面的 ``import`` 转发到 quant_app/run_worker 子包,确保:
  - 任何对 backend.main 的旧引用都仍可用
  - 业务代码全部下沉到 quant_app/{serve_api,run_worker,main}.py

Usage:
    python backend/main.py                       # all 模式(默认)
    python backend/main.py --mode api            # 仅 API server
    python backend/main.py --mode worker         # 仅 Scheduler + Monitor + Runner
    python backend/main.py --mode both           # all 别名
    python backend/main.py --mode scheduler      # worker 别名
"""

from __future__ import annotations

import os
import sys

# 把项目根加入 sys.path,让 ``import quant_app`` 在直接执行本脚本时也能解析
_PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ_DIR not in sys.path:
    sys.path.insert(0, _PROJ_DIR)

# 业务符号统一从 quant_app 导出
from quant_app.main import (  # noqa: F401
    main,
    setup_logging,
    get_monitor,
    get_broker,
    _monitor,
    _broker,
)
from quant_app.run_worker import (  # noqa: F401
    Scheduler,
    is_trading_day,
    _build_trade_calendar,
    _trade_calendar,
    _trade_calendar_date,
    _acquire_pid_lock,
    _release_pid_lock,
    wait_until_next,
)
from quant_app.serve_api import start_api_server  # noqa: F401


if __name__ == '__main__':
    main()

"""
共享 fixtures:构造一个最小化 monitor-like 对象,用于直接调 Mixin 方法。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

# backend/ 加入 sys.path,让 mixin 内 'from services.* import' 能解析
_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'backend',
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


@pytest.fixture
def monitor():
    """
    最小化 IntradayMonitor-like 对象,提供 Mixin 方法所需的全部属性。
    具体方法通过 Mixin.method.__get__(mon) 绑定调用。
    """
    m = MagicMock()
    m._svc = MagicMock()
    m._broker = MagicMock()
    m._llm = None
    m._strategy_runner = None
    m._cooldown = MagicMock()
    m._cooldown.can_fire.return_value = True
    m._selector_cache = []
    m._selector_loaded_date = ''
    m._selector_top_n = 5
    m._params_cache = {}
    m._params_cache_date = ''
    m._sentiment_cache = {}
    m._sentiment_cache_date = ''
    m._market_regime = {}
    m._peak_equity = 0.0
    m._risk_warn_fired = False
    m._risk_stop_fired = False
    m._dd_warn = 0.08
    m._dd_stop = 0.12
    m._kelly_pct = 0.10
    m._kelly_last_updated = ''
    m._trading_mode = 'simulation'
    m._health_check_date = ''
    m._max_pos_pct = 0.20
    m._scan_count = 0
    m._last_scan_symbol = ''
    m._last_scan_time = ''
    m._signal_log = []
    m._skip_log = []
    m._llm_review_log = []
    m._error_count = 0
    m._last_error = ''
    return m

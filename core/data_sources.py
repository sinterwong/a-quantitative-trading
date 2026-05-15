"""
core/data_sources.py — 市场快照数据（Gateway 统一出口）

本模块仅保留 MarketSnapshot dataclass，
数据聚合逻辑已删除（如需使用，直接在业务层调用 Gateway）。

保留的类：
  - MarketSnapshot:  市场快照 dataclass（无网络调用，纯数据）
  - NorthBoundDataSource: 北向资金（内部服务 cached_kamt，无外部直连）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Any
import os
import sys
import time

import pandas as pd

logger = logging.getLogger('data_sources')


# ─── Market Snapshot ─────────────────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    """
    统一市场快照（合并所有外部数据）。
    用于信号生成前的市场环境判断。
    """
    timestamp: datetime = field(default_factory=datetime.now)

    # 外盘
    sp500_change_pct: float = 0       # S&P 500 期货涨跌幅（%）
    nasdaq_change_pct: float = 0      # Nasdaq 期货涨跌幅（%）
    vix: float = 0                    # VIX 恐慌指数
    hsih_change_pct: float = 0        # 恒生指数涨跌幅（%）

    # A 股
    ashare_change_pct: float = 0       # 上证/深证近期涨跌（%）

    # 北向
    north_net_yi: float = 0            # 北向资金净流入（亿元）

    # 内部
    atr_ratio: float = 0               # 市场 ATR ratio（来自 regime_detector）

    def is_us_bullish(self) -> bool:
        """美股期货上涨 → A 股高开概率"""
        return self.sp500_change_pct > 0.3

    def is_us_bearish(self) -> bool:
        return self.sp500_change_pct < -0.3

    def is_hk_bullish(self) -> bool:
        return self.hsih_change_pct > 0.5

    def is_high_volatility(self) -> bool:
        """VIX > 20 → 高波动"""
        return self.vix > 20

    def is_north_inflow(self) -> bool:
        return self.north_net_yi > 10  # 亿


# ─── NorthBound ───────────────────────────────────────────────────────────────

class NorthBoundDataSource:
    """
    北向资金数据（内部服务 cached_kamt）。
    fetch_latest() → 当日北向净流入（亿元）
    """

    name = 'NorthBound'

    def __init__(self):
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
        self._cache_ttl: int = 60  # 秒

    def fetch_latest(self) -> Dict[str, Any]:
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
            from services.data_cache import cached_kamt
            kamt = cached_kamt()
            result = {
                'timestamp': datetime.now(),
                'net_north_yi': kamt.get('net_north_yi', 0),
                'net_south_yi': kamt.get('net_south_yi', 0),
                'north_direction': 'BUY' if kamt.get('net_north_yi', 0) > 0 else 'SELL',
                'source': kamt.get('source', 'unknown'),
                'stale': kamt.get('stale', False),
            }
            self._cache = result
            self._cache_time = now
            return result
        except Exception as e:
            return {'symbol': 'KAMT', 'error': str(e), 'source': 'failed'}

    def fetch_history(self, days: int = 5) -> pd.DataFrame:
        # KAMT 历史需从数据库读取，当前返回空
        return pd.DataFrame()
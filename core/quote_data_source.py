# -*- coding: utf-8 -*-
"""
quote_data_source.py — 行情数据源抽象接口
==========================================

定义所有行情数据源的统一抽象层：
  - QuoteData: 统一实时行情数据类
  - QuoteDataSource ABC: 行情数据源抽象基类
  - 工具函数: detect_market, normalize_to_sina, normalize_to_tencent

设计目标：
  - 腾讯和新浪作为互补的默认数据源
  - 消除 5 处重复的新浪 HTTP 代码
  - 提供统一的市场检测和代码转换

Usage:
  from core.quote_data_source import QuoteDataSource, QuoteData, detect_market
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from .symbol_utils import (
    _safe_float, _safe_int, detect_market,
    normalize_to_sina, normalize_to_tencent,
)

__all__ = [
    'QuoteData', 'QuoteDataSource',
    '_safe_float', '_safe_int', 'detect_market',
    'normalize_to_sina', 'normalize_to_tencent',
]


# ─── 统一行情数据类 ──────────────────────────────────────────────────────────


@dataclass
class QuoteData:
    """统一实时行情快照（跨市场通用格式）"""

    symbol: str           # 标准化后的代码，如 'sh600519', 'hk00700', 'usAAPL'
    name: str = ""        # 名称
    code: str = ""        # 纯代码，如 '600519', '00700', 'AAPL'
    market: str = ""      # A / INDEX / HK / US

    # 价格
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    avg_price: float = 0.0

    # 涨跌
    change: float = 0.0       # 涨跌额
    pct_change: float = 0.0   # 涨跌幅 (%)

    # 成交
    volume: float = 0.0       # 成交量（股/份）
    amount: float = 0.0       # 成交额（元/港元/美元）
    turnover_rate: float = 0.0  # 换手率 (%)

    # 盘口
    bid1_price: float = 0.0
    bid1_vol: float = 0.0
    ask1_price: float = 0.0
    ask1_vol: float = 0.0

    # 基本面
    pe_ttm: float = 0.0
    pb: float = 0.0
    dividend_yield: float = 0.0
    market_cap: float = 0.0    # 总市值（亿元/亿港元/亿美元）
    float_cap: float = 0.0     # 流通市值

    # 限制
    limit_up: float = 0.0
    limit_down: float = 0.0
    amplitude: float = 0.0     # 振幅 (%)

    # 52 周
    high_52w: float = 0.0
    low_52w: float = 0.0

    # 元数据
    volume_ratio: float = 0.0  # 量比
    currency: str = ""         # CNY / HKD / USD
    timestamp: str = ""        # 原始时间戳字符串
    source: str = ""           # 数据源标识: 'tencent' / 'sina'

    @property
    def is_valid(self) -> bool:
        return self.price > 0

    @property
    def day_change(self) -> float:
        """当日涨跌额"""
        return self.price - self.prev_close


# ─── 抽象基类 ────────────────────────────────────────────────────────────────


class QuoteDataSource(ABC):
    """行情数据源抽象接口"""

    name: str = "QuoteDataSource"

    @abstractmethod
    def fetch_quote(self, symbol: str) -> Optional[QuoteData]:
        """获取单只标的实时行情"""

    @abstractmethod
    def fetch_quotes(self, symbols: List[str]) -> Dict[str, QuoteData]:
        """批量获取实时行情"""

    @abstractmethod
    def fetch_daily_kline(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取日 K 线数据。

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """

    @abstractmethod
    def fetch_minute_kline(
        self,
        symbol: str,
        period: str = "15m",
        limit: int = 100,
    ) -> pd.DataFrame:
        """
        获取分钟 K 线数据。

        Args:
            period: '1m', '5m', '15m', '30m', '60m'
            limit: 返回的 K 线根数

        Returns:
            DataFrame with columns: datetime, open, high, low, close, volume
        """

    @abstractmethod
    def supported_markets(self) -> List[str]:
        """返回支持的市场列表，如 ['A', 'INDEX', 'HK']"""



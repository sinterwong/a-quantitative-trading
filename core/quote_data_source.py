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


# ─── 工具函数 ────────────────────────────────────────────────────────────────


def detect_market(symbol: str) -> str:
    """
    检测标的市场类型。

    支持格式：
      sh600519, sz000001, 600519.SH, 000001.SZ → A
      sh000001, sz399006, 000001.SH             → INDEX
      hk00700, HK:00700, 00700.HK              → HK
      usAAPL, US:AAPL                           → US

    Returns: 'A' | 'INDEX' | 'HK' | 'US'
    """
    s = symbol.strip()

    # HK:xxx / US:xxx 格式
    if s.upper().startswith("HK:"):
        return "HK"
    if s.upper().startswith("US:"):
        return "US"

    # xxx.HK 格式
    if s.upper().endswith(".HK"):
        return "HK"

    # sh/sz 前缀
    lower = s.lower()
    if lower.startswith("hk"):
        return "HK"
    if lower.startswith("us"):
        return "US"
    if lower.startswith(("sh000", "sz399")):
        return "INDEX"
    if lower.startswith(("sh", "sz")):
        return "A"

    # xxx.SH / xxx.SZ 后缀
    upper = s.upper()
    if upper.endswith(".SH") or upper.endswith(".SZ"):
        # 需要检查是否是指数
        code = s[:-3].strip()
        if code.startswith("000") and upper.endswith(".SH"):
            return "INDEX"
        if code.startswith("399") and upper.endswith(".SZ"):
            return "INDEX"
        return "A"

    # 纯数字
    if s.isdigit():
        if s.startswith(("000", "399")):
            return "INDEX"
        return "A"

    # 纯字母 → 美股
    if s.isalpha():
        return "US"

    return "A"


def normalize_to_sina(symbol: str) -> str:
    """
    将任意格式的标的代码转换为新浪格式。

    新浪 A 股: sh600519 / sz000001
    新浪港股: hk00700
    新浪美股: gb_aapl

    Examples:
        '600519.SH' → 'sh600519'
        '000001.SZ' → 'sz000001'
        'HK:00700'  → 'hk00700'
        'US:AAPL'   → 'gb_aapl'
        'sh600519'  → 'sh600519'
    """
    s = symbol.strip()
    upper = s.upper()

    # HK:xxx 格式
    if upper.startswith("HK:"):
        code = s[3:].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # US:xxx 格式
    if upper.startswith("US:"):
        return f"gb_{s[3:].strip().lower()}"

    # xxx.HK 格式
    if upper.endswith(".HK"):
        code = s[:-3].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # xxx.SH / xxx.SZ 格式
    if upper.endswith(".SH"):
        return f"sh{s[:-3].strip()}"
    if upper.endswith(".SZ"):
        return f"sz{s[:-3].strip()}"

    # 已经是 sh/sz 格式
    lower = s.lower()
    if lower.startswith(("sh", "sz")):
        return lower

    # hk 前缀（港股）
    if lower.startswith("hk"):
        return lower

    # us 前缀（美股）→ gb_ 格式
    if lower.startswith("us"):
        code = s[2:]
        return f"gb_{code.lower()}"

    # 纯数字 → A 股
    if s.isdigit():
        if s.startswith(("60", "68", "5")):
            return f"sh{s}"
        return f"sz{s}"

    # 纯字母 → 美股
    if s.isalpha():
        return f"gb_{s.lower()}"

    return lower


def normalize_to_tencent(symbol: str) -> str:
    """
    将任意格式的标的代码转换为腾讯格式。

    腾讯 A 股: sh600519 / sz000001
    腾讯港股: hk00700
    腾讯美股: usAAPL（区分大小写）

    Examples:
        '600519.SH' → 'sh600519'
        '000001.SZ' → 'sz000001'
        'HK:00700'  → 'hk00700'
        'US:AAPL'   → 'usAAPL'
        'sh600519'  → 'sh600519'
    """
    s = symbol.strip()
    upper = s.upper()

    # HK:xxx 格式
    if upper.startswith("HK:"):
        code = s[3:].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # US:xxx 格式
    if upper.startswith("US:"):
        return f"us{s[3:].strip()}"

    # xxx.HK 格式
    if upper.endswith(".HK"):
        code = s[:-3].strip()
        if code.isdigit():
            return f"hk{code.zfill(5)}"
        return f"hk{code}"

    # xxx.SH / xxx.SZ 格式
    if upper.endswith(".SH"):
        return f"sh{s[:-3].strip()}"
    if upper.endswith(".SZ"):
        return f"sz{s[:-3].strip()}"

    # 已经是 us/hk 格式（保留大小写）
    if s.lower().startswith(("us", "hk")):
        return s

    # 已经是 sh/sz 格式（A 股不区分大小写）
    lower = s.lower()
    if lower.startswith(("sh", "sz")):
        return lower

    # 纯数字
    if s.isdigit():
        if s.startswith(("60", "68", "5")):
            return f"sh{s}"
        return f"sz{s}"

    # 纯字母 → 美股（保留大小写）
    if s.isalpha():
        return f"us{s.upper()}"

    return lower


def tencent_quote_to_quote_data(tq) -> QuoteData:
    """将 TencentQuote 转换为 QuoteData"""
    return QuoteData(
        symbol=tq.symbol, name=tq.name, code=tq.code, market=tq.market,
        price=tq.price, prev_close=tq.prev_close, open=tq.open,
        high=tq.high, low=tq.low, avg_price=tq.avg_price,
        change=tq.change, pct_change=tq.pct_change,
        volume=tq.volume, amount=tq.amount, turnover_rate=tq.turnover_rate,
        bid1_price=tq.bid1_price, bid1_vol=tq.bid1_vol,
        ask1_price=tq.ask1_price, ask1_vol=tq.ask1_vol,
        pe_ttm=tq.pe_ttm, pb=tq.pb, dividend_yield=tq.dividend_yield,
        market_cap=tq.market_cap, float_cap=tq.float_cap,
        limit_up=tq.limit_up, limit_down=tq.limit_down, amplitude=tq.amplitude,
        high_52w=tq.high_52w, low_52w=tq.low_52w,
        volume_ratio=tq.volume_ratio, currency=tq.currency,
        timestamp=tq.timestamp, source='tencent',
    )


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全转换为 float"""
    if val is None:
        return default
    try:
        s = str(val).strip()
        if s in ("", "-", "--"):
            return default
        f = float(s)
        return f if f == f else default  # NaN check
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    """安全转换为 int"""
    f = _safe_float(val, float(default))
    return int(f)

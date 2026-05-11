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
    'SectorData', 'SectorConstituentData',
    '_safe_float', '_safe_int', 'detect_market',
    'normalize_to_sina', 'normalize_to_tencent',
]


# ─── 统一行情数据类 ──────────────────────────────────────────────────────────


def _default_for_field(field_name: str):
    """返回各字段的默认值（用于判断是否真的"无数据"）"""
    return '' if field_name in ('currency', 'timestamp', 'symbol', 'name', 'code', 'market') else 0.0


@dataclass
class QuoteData:
    """统一实时行情快照（跨市场通用格式）

    字段来源追踪：
      每个字段最终值可能来自不同数据源，通过 _field_sources 追踪。
      merge() 方法自动从多个 QuoteData 中选取最优值。
    """

    symbol: str = ""           # 标准化后的代码，如 'sh600519', 'hk00700', 'usAAPL'
    name: str = ""            # 名称
    code: str = ""            # 纯代码，如 '600519', '00700', 'AAPL'
    market: str = ""           # A / INDEX / HK / US

    # 价格
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    avg_price: float = 0.0

    # 涨跌
    change: float = 0.0        # 涨跌额
    pct_change: float = 0.0    # 涨跌幅 (%)

    # 成交
    volume: float = 0.0        # 成交量（股/份）
    amount: float = 0.0        # 成交额（元/港元/美元）
    turnover_rate: float = 0.0 # 换手率 (%)

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
    amplitude: float = 0.0      # 振幅 (%)

    # 52 周
    high_52w: float = 0.0
    low_52w: float = 0.0

    # 元数据
    volume_ratio: float = 0.0  # 量比
    currency: str = ""         # CNY / HKD / USD
    timestamp: str = ""        # 原始时间戳字符串
    source: str = ""           # 主来源标识: 'tencent' / 'sina' / 'eastmoney'

    # ── 字段来源追踪 ──────────────────────────────────────────────────────
    # {字段名: 来源标识}，记录每个字段最终使用了哪个源的数据
    _field_sources: Dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def is_valid(self) -> bool:
        return self.price > 0

    @property
    def day_change(self) -> float:
        """当日涨跌额"""
        return self.price - self.prev_close

    def merge(self, other: "QuoteData", priority: str = "tencent") -> "QuoteData":
        """
        合并另一个 QuoteData，取最优值（优先用 priority 指定来源的值）。

        合并策略：
          - priority 来源有值（!= 默认值）→ 用 priority 的值
          - priority 来源无值，但另一个来源有值 → 用另一个来源的值
          - 都不足 → 保留当前对象（self）的值

        Args:
            other:   另一个 QuoteData（通常是备源）
            priority: 优先使用的来源标识（默认 'tencent'）

        Returns:
            合并后的 QuoteData（不修改原对象）
        """
        if not isinstance(other, QuoteData):
            return self

        # 收集所有字段名
        all_fields = [f.name for f in self.__dataclass_fields__.values()
                      if not f.name.startswith('_') and f.name not in ('symbol', 'market', 'source')]

        result_fields = {}
        result_sources = dict(self._field_sources)  # 继承 self 的来源记录

        for fname in all_fields:
            self_val = getattr(self, fname)
            other_val = getattr(other, fname)
            self_default = _default_for_field(fname)

            # 判断是否"有值"
            self_has = (self_val != self_default)
            other_has = (other_val != self_default)

            if self_val == other_val:
                chosen = self_val
                chosen_src = self._field_sources.get(fname, self.source or 'unknown')
            elif self_val == self_default and other_val != self_default:
                chosen = other_val
                chosen_src = other.source or 'unknown'
            elif self_val != self_default and other_val == self_default:
                chosen = self_val
                chosen_src = self._field_sources.get(fname, self.source or 'unknown')
            else:
                # 两者都有值，按 priority 选
                if other.source == priority:
                    chosen = other_val
                    chosen_src = other.source or 'unknown'
                else:
                    chosen = self_val
                    chosen_src = self._field_sources.get(fname, self.source or 'unknown')

            result_fields[fname] = chosen
            result_sources[fname] = chosen_src

        # symbol / market 以 self 为准；source 标注主次来源（不用累积字符串）
        result_fields['symbol'] = self.symbol or other.symbol
        result_fields['market'] = self.market or other.market
        if self.source and other.source and self.source != other.source:
            result_fields['source'] = f"{priority}({self._get_origin()})+{other.source}"
        else:
            result_fields['source'] = self.source or other.source or priority

        merged = QuoteData(**result_fields)
        merged._field_sources = result_sources
        return merged

    def field_source(self, field_name: str) -> str:
        """查询某个字段的数据来源（未记录则返回 self.source）"""
        return self._field_sources.get(field_name, self.source)

    def _get_origin(self) -> str:
        """
        从 source 字符串提取"原始单一来源"标识。

        source 可能是：
          - 单一来源:  'tencent'             → 返回 'tencent'
          - 合并来源:  'tencent(sina)+sina'  → 返回 'sina'（括号内是备源）

        用于 merge() 中避免 source 字符串无限嵌套累积。
        """
        src = self.source or ''
        lpar = src.rfind('(')
        rpar = src.rfind(')')
        if lpar >= 0 and rpar >= 0 and rpar > lpar:
            return src[lpar + 1:rpar]
        return src


# ─── 板块数据类 ──────────────────────────────────────────────────────────────


@dataclass
class SectorData:
    """板块行情数据（跨数据源统一格式）"""

    bk_code: str = ""       # 板块代码，如 'SINA_GNhwqc', 'BK0716'
    name: str = ""          # 板块名称，如 '华为汽车'
    change_pct: float = 0.0 # 涨跌幅 (%)
    net_flow: float = 0.0   # 资金净流入（元），北向/主力
    amount: float = 0.0     # 成交额（元）
    rank_perf: int = 0      # 涨幅排名（1=最强）
    rank_flow: int = 0      # 资金流排名（1=最强）
    source: str = ""         # 数据来源：'eastmoney' / 'sina'
    timestamp: str = ""      # 数据时间戳

    @property
    def is_valid(self) -> bool:
        return bool(self.bk_code)


@dataclass
class SectorConstituentData:
    """板块成分股数据"""

    symbol: str = ""         # 标准化代码，如 'sh600519'
    name: str = ""          # 股票名称
    price: float = 0.0      # 当前价
    change_pct: float = 0.0 # 涨跌幅 (%)
    amount: float = 0.0     # 成交额
    volume: float = 0.0     # 成交量
    source: str = ""        # 数据来源

    @property
    def is_valid(self) -> bool:
        return bool(self.symbol)


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



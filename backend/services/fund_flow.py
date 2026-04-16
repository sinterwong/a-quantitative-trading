# -*- coding: utf-8 -*-
"""
fund_flow.py — 主力资金流服务
==========================

数据来源: AkShare
  - stock_individual_fund_flow(stock)  → 大盘/指数资金流（注意：返回沪深300指数数据，非个股）
  - stock_market_fund_flow()            → 两市（沪深）资金流汇总

⚠️ 重要说明：
  AkShare 的 stock_individual_fund_flow() 无论传入什么 stock 代码，
  都返回沪深300指数的主力资金流数据（收盘价/涨跌幅是沪深300指数），
  因此 get_stock_fund_flow() 实际获取的是"大盘资金流"，不是个股。
  若需个股资金流，需使用 level2 数据或其他数据源。

资金分类（按成交量档位划分）：
  超大单: >100万手 或 成交额>1亿元
  大单:  20~100万手
  中单:  5~20万手
  小单:  <5万手

主力净流入 = 超大单 + 大单（特大单+大单合计）

Usage:
  from services.fund_flow import FundFlowService
  fs = FundFlowService()

  # 大盘资金流（最近N日，沪深300指数）
  flow = fs.get_stock_fund_flow("000300")

  # 两市大盘资金流汇总
  market = fs.get_market_fund_flow()

  # 大盘主力净流入摘要（用于选股评分）
  summary = fs.get_main_net_summary("000300")
"""

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from threading import RLock
from typing import Dict, List, Optional, Any

import pandas as pd

# 禁用代理
for _key in list(os.environ.keys()):
    if 'proxy' in _key.lower():
        del os.environ[_key]

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False

logger = logging.getLogger('fund_flow')

# ─── 数据类型 ────────────────────────────────────────────────────────────────


@dataclass
class StockFundFlow:
    """
    个股/大盘资金流快照。

    单位说明:
      - 净额: 元（需 /1e8 转为"亿元"）
      - 占比: %（已 /100 处理，可用 f"{v:.2%}" 格式化）
    """
    code: str           # 股票代码或 "000300"（沪深300大盘）
    name: str           # 名称
    date: str           # 日期 "YYYY-MM-DD"
    close: float        # 收盘价
    change_pct: float   # 涨跌幅(%)

    # 各档位净流入（单位：元）
    super_net: float       # 超大单净流入
    super_pct: float       # 超大单净流入占比
    large_net: float       # 大单净流入
    large_pct: float       # 大单净流入占比
    medium_net: float      # 中单净流入
    medium_pct: float      # 中单净流入占比
    small_net: float       # 小单净流入
    small_pct: float       # 小单净流入占比

    @property
    def main_net(self) -> float:
        """主力净流入 = 超大单 + 大单（单位：元）"""
        return self.super_net + self.large_net

    @property
    def main_pct(self) -> float:
        """主力净流入占比"""
        total = self.super_net + self.large_net + self.medium_net + self.small_net
        if total == 0:
            return 0.0
        return (self.main_net / total) * 100.0

    def to_dict(self) -> Dict:
        d = {
            'code': self.code,
            'name': self.name,
            'date': self.date,
            'close': self.close,
            'change_pct': self.change_pct,
            'main_net': round(self.main_net, 2),          # 元
            'main_pct': round(self.main_pct, 2),            # %
            'super_net': round(self.super_net, 2),
            'super_pct': round(self.super_pct, 2),
            'large_net': round(self.large_net, 2),
            'large_pct': round(self.large_pct, 2),
            'medium_net': round(self.medium_net, 2),
            'medium_pct': round(self.medium_pct, 2),
            'small_net': round(self.small_net, 2),
            'small_pct': round(self.small_pct, 2),
        }
        return d


@dataclass
class SectorFundFlow:
    """板块资金流"""
    sector_code: str    # 板块代码
    sector_name: str    # 板块名称
    rank: int           # 涨跌幅排名
    change_pct: float   # 涨跌幅(%)
    main_net: float     # 主力净流入（元）
    turnover: float     # 换手率(%)
    volume_ratio: float  # 量比


# ─── 字段名映射（兼容 AkShare 中英文列名）──────────────────────────────────

# 原始中文列名（AkShare 返回）
_FUND_FIELD_MAP = {
    # 个股/市场资金流列名映射（直接从列名字符串匹配）
    '日期': 'date',
    '收盘价': 'close',          # 注：个股接口返回的是沪深300指数收盘价
    '涨跌幅': 'change_pct',
    # 主力 = 超大单 + 大单（特大单+大单合计）
    '主力净流入-净额': 'main_net',
    '主力净流入-净占比': 'main_pct',
    # 超大单
    '超大单净流入-净额': 'super_net',
    '超大单净流入-净占比': 'super_pct',
    # 大单
    '大单净流入-净额': 'large_net',
    '大单净流入-净占比': 'large_pct',
    # 中单
    '中单净流入-净额': 'medium_net',
    '中单净流入-净占比': 'medium_pct',
    # 小单
    '小单净流入-净额': 'small_net',
    '小单净流入-净占比': 'small_pct',
}


def _parse_fund_df(df: pd.DataFrame, code: str, name: str = '') -> List[StockFundFlow]:
    """
    将 AkShare 返回的 fund flow DataFrame 解析为 StockFundFlow 列表。

    列名匹配：直接用列名字符串匹配 _FUND_FIELD_MAP
    日期格式：降序（最新在前）
    """
    if df is None or df.empty:
        return []

    # Step 1: 标准化列名（直接匹配中文列名字符串）
    rename_map = {}
    for col in df.columns:
        if col in _FUND_FIELD_MAP:
            rename_map[col] = _FUND_FIELD_MAP[col]

    df = df.rename(columns=rename_map)

    # Step 2: 验证必要列
    required = ['date', 'close', 'change_pct', 'main_net', 'main_pct']
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.warning("[FundFlow] 缺少列 %s，可用列: %s", missing, list(df.columns))
        return []

    # Step 3: 数值类型转换
    num_cols = ['close', 'change_pct', 'main_net', 'main_pct',
                 'super_net', 'super_pct', 'large_net', 'large_pct',
                 'medium_net', 'medium_pct', 'small_net', 'small_pct']
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # Step 4: 按日期降序
    if 'date' in df.columns:
        df = df.sort_values('date', ascending=False).reset_index(drop=True)

    # Step 5: 构建对象
    records = []
    for _, row in df.iterrows():
        if pd.isna(row.get('date')):
            continue
        try:
            records.append(StockFundFlow(
                code=code,
                name=name,
                date=str(row['date'])[:10],
                close=float(row.get('close', 0) or 0),
                change_pct=float(row.get('change_pct', 0) or 0),
                super_net=float(row.get('super_net', 0) or 0),
                super_pct=float(row.get('super_pct', 0) or 0),
                large_net=float(row.get('large_net', 0) or 0),
                large_pct=float(row.get('large_pct', 0) or 0),
                medium_net=float(row.get('medium_net', 0) or 0),
                medium_pct=float(row.get('medium_pct', 0) or 0),
                small_net=float(row.get('small_net', 0) or 0),
                small_pct=float(row.get('small_pct', 0) or 0),
            ))
        except Exception:
            continue

    return records


# ─── 简易内存缓存 ────────────────────────────────────────────────────────

class _FundFlowCache:
    """资金流专用缓存（线程安全，TTL）"""
    _lock = RLock()
    _store: Dict[str, Dict[str, Any]] = {}  # {key: {'expires_at': float, 'value': Any}}

    @classmethod
    def get(cls, key: str) -> Optional[Any]:
        with cls._lock:
            entry = cls._store.get(key)
            if entry and time.monotonic() < entry['expires_at']:
                return entry['value']
            return None

    @classmethod
    def set(cls, key: str, value: Any, ttl: float = 600):
        with cls._lock:
            cls._store[key] = {'value': value, 'expires_at': time.monotonic() + ttl}

    @classmethod
    def get_or_fetch(cls, key: str, fetch_fn, ttl: float = 600) -> Any:
        cached = cls.get(key)
        if cached is not None:
            return cached
        value = fetch_fn()
        if value is not None:
            cls.set(key, value, ttl)
        return value


# ─── 主力资金流服务 ────────────────────────────────────────────────────────


class FundFlowService:
    """
    主力资金流服务。

    功能：
      1. 个股资金流（支持获取近N日/累计数据）
      2. 大盘市场资金流（上证+深证汇总）
      3. 板块资金流排名（需要网络畅通）

    缓存策略：
      - 个股资金流: 10 分钟 TTL（日内变化慢）
      - 大盘资金流: 30 分钟 TTL
    """

    # 大盘指数代码
    MARKET_CODE = '000300'  # 沪深300作为大盘代表

    def __init__(self):
        if not AKSHARE_AVAILABLE:
            raise ImportError("[FundFlowService] AkShare 未安装: pip install akshare")

    # ── 公开 API ────────────────────────────────────────────────────────

    def get_stock_fund_flow(
        self,
        stock_code: str,
        days: int = 5,
    ) -> List[StockFundFlow]:
        """
        获取个股资金流（最近N日）。

        Args:
            stock_code: 股票代码，如 "600900"
            days: 获取最近N个交易日数据（默认5日）

        Returns:
            List[StockFundFlow]，按日期降序（最新在前）

        注意：
            AkShare 的 stock_individual_fund_flow 以"时间序列"形式返回，
            每行是一个交易日的资金流，请参考使用。
        """
        cache_key = f"fund_flow_{stock_code}_{days}"

        def fetch():
            df = ak.stock_individual_fund_flow(stock=stock_code)
            if df is None or df.empty:
                return None
            records = _parse_fund_df(df, code=stock_code)
            return records[:days]

        cached = _FundFlowCache.get(cache_key)
        if cached is not None:
            logger.debug("[FundFlow] 缓存命中 %s", cache_key)
            return cached

        try:
            records = fetch()
            if records is None or not records:
                return []
            logger.info("[FundFlow] 获取 %s 资金流 %d 日: %s",
                       stock_code, len(records),
                       [(r.date, f"{r.main_net/1e8:.2f}亿") for r in records[:3]])
            _FundFlowCache.set(cache_key, records, ttl=600)
            return records
        except Exception as e:
            logger.warning("[FundFlow] 获取 %s 资金流失败: %s", stock_code, e)
            return []

    def get_market_fund_flow(self, days: int = 5) -> Dict:
        """
        获取大盘市场资金流汇总。

        Returns:
            Dict，包含:
              - sh: 上证主力净流入（亿元）
              - sz: 深证主力净流入（亿元）
              - main_net: 两市合计主力净流入（亿元）
              - date: 日期
        """
        cache_key = f"fund_flow_market_{days}"
        cached = _FundFlowCache.get(cache_key)
        if cached is not None:
            return cached

        try:
            df = ak.stock_market_fund_flow()
            if df is None or df.empty:
                return {}

            # 列名标准化（直接匹配中文字段名）
            rename = {}
            for col in df.columns:
                if col in _FUND_FIELD_MAP:
                    rename[col] = _FUND_FIELD_MAP[col]
                elif col.startswith('上证'):
                    if '收盘价' in col:
                        rename[col] = 'sh_close'
                    elif '涨跌幅' in col:
                        rename[col] = 'sh_change'
                elif col.startswith('深证'):
                    if '收盘价' in col:
                        rename[col] = 'sz_close'
                    elif '涨跌幅' in col:
                        rename[col] = 'sz_change'

            df = df.rename(columns=rename)

            # 按日期降序，取最新
            if 'date' in df.columns:
                df = df.sort_values('date', ascending=False).reset_index(drop=True)
            latest = df.iloc[0]

            result = {
                'date': str(latest.get('date', ''))[:10],
                'sh_close': latest.get('sh_close'),
                'sh_change': latest.get('sh_change'),
                'sz_close': latest.get('sz_close'),
                'sz_change': latest.get('sz_change'),
                'main_net': round(float(latest.get('main_net', 0) or 0) / 1e8, 2),  # 亿元
                'main_pct': latest.get('main_pct', 0),  # %
            }

            _FundFlowCache.set(cache_key, result, ttl=1800)
            logger.info("[FundFlow] 大盘资金流: %s", result)
            return result

        except Exception as e:
            logger.warning("[FundFlow] 获取大盘资金流失败: %s", e)
            return {}

    def get_main_net_summary(self, stock_code: str) -> Dict:
        """
        获取个股主力净流入摘要（用于选股评分）。

        Returns:
            {
                'main_net_1d': float,   # 今日主力净流入（元）
                'main_net_5d': float,   # 5日累计主力净流入（元）
                'main_net_10d': float,  # 10日累计主力净流入（元）
                'signal': str,          # 'strong_inflow' / 'inflow' / 'neutral' / 'outflow' / 'strong_outflow'
            }
        """
        flow = self.get_stock_fund_flow(stock_code, days=10)
        if not flow:
            return {'signal': 'unknown', 'main_net_1d': 0, 'main_net_5d': 0, 'main_net_10d': 0}

        # 今日
        net_1d = flow[0].main_net if len(flow) > 0 else 0
        # 5日累计
        net_5d = sum(r.main_net for r in flow[:5]) if len(flow) >= 5 else sum(r.main_net for r in flow)
        # 10日累计
        net_10d = sum(r.main_net for r in flow)

        # 信号判断（阈值待验证，根据实际数据调整）
        # 以"亿元"为单位
        net_1d_yi = net_1d / 1e8
        net_5d_yi = net_5d / 1e8

        if net_1d_yi > 5:
            signal = 'strong_inflow'
        elif net_1d_yi > 1:
            signal = 'inflow'
        elif net_1d_yi < -5:
            signal = 'strong_outflow'
        elif net_1d_yi < -1:
            signal = 'outflow'
        else:
            signal = 'neutral'

        return {
            'code': stock_code,
            'date': flow[0].date,
            'main_net_1d': round(net_1d, 2),
            'main_net_5d': round(net_5d, 2),
            'main_net_10d': round(net_10d, 2),
            'signal': signal,
        }
